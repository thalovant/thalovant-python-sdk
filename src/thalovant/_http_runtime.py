"""Cookie-affine HTTPS carrier for a HiveMind Noise channel."""

from __future__ import annotations

import base64
import threading
import time
import uuid
from typing import Any

from ._noise_runtime import NoiseChannel
from .errors import ThalovantConnectionError, ThalovantTimeoutError


class HTTPNoiseClient:
    def __init__(self, transport: Any) -> None:
        import requests

        self.transport = transport
        self.useragent = transport.useragent
        self.site_id = transport.identity.site_id
        self.session_id = f"thalovant-python-{uuid.uuid4().hex}"
        self.base_url = transport.identity.endpoint_base()
        self.auth = base64.b64encode(f"{self.useragent}:{transport.identity.access_key}".encode()).decode("ascii")
        self.connected = threading.Event()
        self._admitted = False
        self.handshake_event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._request_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._session = requests.Session()
        # Keep affinity across explicit reconnects as well as polling requests.
        # A new adapter must return to the replica whose admission it owns.
        with transport._lifecycle_lock:
            if not hasattr(transport, "_http_cookie_jar"):
                transport._http_cookie_jar = requests.cookies.RequestsCookieJar()
            self._session.cookies = transport._http_cookie_jar
        self._session.headers.update({"User-Agent": self.useragent})
        self._handlers: dict[str, list[Any]] = {}
        self._hive_handlers: dict[str, list[Any]] = {}
        self.thalovant_last_error: BaseException | None = None
        self._deadline: float | None = None
        self.channel = NoiseChannel(
            transport.identity, state_dir=transport.noise_state_dir, pin_id=self.base_url,
            hello={"msg_type": "hello", "payload": {"pubkey": "", "site_id": self.site_id,
                "session": {"session_id": self.session_id}}, "metadata": {}, "route": []},
            write=self._write,
        )

    def request(self, path: str, *, method: str = "GET", data: Any = None) -> dict[str, Any]:
        with self._request_lock:
            remaining = self.transport.send_timeout
            if self._deadline is not None:
                remaining = min(remaining, self._deadline - time.monotonic())
            if remaining <= 0:
                raise ThalovantTimeoutError("HiveMind HTTP Noise handshake timed out.")
            from requests import RequestException, Timeout

            try:
                response = self._session.request(
                    method, f"{self.base_url}{path}", params={"authorization": self.auth}, data=data,
                    timeout=remaining, verify=not self.transport.self_signed, allow_redirects=False,
                )
            except Timeout:
                detail = "HiveMind HTTP Noise handshake timed out." if self._deadline is not None else "HiveMind HTTP request timed out."
                # Requests exceptions can embed the authorization query. Suppress
                # their chain so normal formatted tracebacks cannot expose it.
                raise ThalovantTimeoutError(detail) from None
            except RequestException:
                raise ThalovantConnectionError("HiveMind HTTP request failed.") from None
            if 300 <= response.status_code < 400:
                raise ThalovantConnectionError("HiveMind HTTP endpoint redirected the request.")
            if not 200 <= response.status_code < 300:
                raise ThalovantConnectionError(f"HiveMind HTTP request failed with HTTP {response.status_code}.")
            try:
                body = response.json()
            except ValueError:
                raise ThalovantConnectionError("Invalid HiveMind HTTP response.") from None
            if not isinstance(body, dict):
                raise ThalovantConnectionError("Invalid HiveMind HTTP response.")
            # The upstream /disconnect handler uses this exact response when
            # an earlier successful cleanup acknowledgment was lost.
            already_disconnected = path == "/disconnect" and body == {"error": "Already Disconnected"}
            if body.get("error") and not already_disconnected:
                raise ThalovantConnectionError("HiveMind HTTP request was refused.")
            return body

    def connect(self) -> None:
        self._deadline = time.monotonic() + self.transport.connect_timeout + self.transport.handshake_timeout
        try:
            # Cleanup must not observe a false admission flag between a
            # successful response and publication of the admission it owns.
            with self._request_lock:
                self._check_current()
                self.request("/connect", method="POST")
                self._admitted = True
            self._check_current()
            self.connected.set()
            with self.transport._lifecycle_lock:
                self._check_current()
                self.transport._mark_transport_open()
            while not self.channel.ready:
                self._check_current()
                self._poll()
                self._check_current()
                if time.monotonic() >= self._deadline:
                    raise ThalovantTimeoutError("HiveMind HTTP Noise handshake timed out.")
                if not self.channel.ready:
                    self._stop.wait(self.transport.handshake_poll_interval)
            self.handshake_event.set()
            self._check_current()
            self._deadline = None
            self._thread = threading.Thread(target=self._run, daemon=True, name="thalovant-http")
            self._thread.start()
        except Exception:
            # The transport lifecycle owns cleanup and retains any failed
            # admission. Do not make a second, untracked disconnect attempt.
            self.connected.clear()
            self.handshake_event.clear()
            raise

    def _check_current(self) -> None:
        if self._stop.is_set() or not self.transport._is_current_client(self):
            raise ThalovantConnectionError("HiveMind HTTP connection was closed or replaced.")

    def _write(self, payload: str | bytes) -> None:
        data = {"message": payload} if isinstance(payload, str) else {
            "message": base64.b64encode(payload).decode("ascii"), "binary": "1",
        }
        self.request("/send_message", method="POST", data=data)

    def _poll(self) -> None:
        messages = self.request("/get_messages").get("messages")
        self._check_current()
        if not isinstance(messages, list):
            raise ThalovantConnectionError("Invalid HiveMind HTTP message response.")
        for raw in messages:
            if not isinstance(raw, str):
                raise ThalovantConnectionError("Invalid HiveMind HTTP cleartext frame.")
            self._receive(raw)
        if self.channel.session is not None:
            messages = self.request("/get_binary_messages").get("b64_messages")
            self._check_current()
            if not isinstance(messages, list):
                raise ThalovantConnectionError("Invalid HiveMind HTTP binary response.")
            for raw in messages:
                if not isinstance(raw, str):
                    raise ThalovantConnectionError("Invalid HiveMind HTTP binary frame.")
                self._receive(base64.b64decode(raw, validate=True))

    def _receive(self, raw: str | bytes) -> None:
        self._check_current()
        message = self.channel.receive(raw)
        if message is None:
            return
        kind = str(getattr(message.msg_type, "value", message.msg_type))
        if kind == "bus":
            for callback in tuple(self._handlers.get(str(message.payload.msg_type), ())):
                callback(message.payload)
        for callback in tuple(self._hive_handlers.get(kind, ())):
            callback(message)

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.transport.handshake_poll_interval):
                self._poll()
        except Exception as exc:
            self.thalovant_last_error = exc
            self.connected.clear()
            self.handshake_event.clear()
            self.channel.close()
            self.transport._fail_current_client(self, exc)

    def emit(self, message: Any) -> Any:
        try:
            return self.channel.send(message)
        except Exception as exc:
            self.thalovant_last_error = exc
            self.connected.clear()
            self.handshake_event.clear()
            self._stop.set()
            self.transport._fail_current_client(self, exc)
            raise

    def close(self) -> None:
        with self._close_lock:
            self._stop.set()
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(timeout=self.transport.send_timeout + 1)
            self.connected.clear()
            self.handshake_event.clear()
            # Noise writers take the channel lock before the request lock.
            # Never hold the request lock while waiting to retire the channel.
            self.channel.close()
            with self._request_lock:
                if self._admitted:
                    try:
                        self._deadline = time.monotonic() + min(2.0, self.transport.send_timeout)
                        reply = self.request("/disconnect", method="POST")
                        if reply != {"error": "Already Disconnected"} and (
                            reply.get("status") != "Disconnected" or reply.get("ok") is False
                        ):
                            raise ThalovantConnectionError("Invalid disconnect acknowledgment.")
                    except Exception:
                        # Keep this session and its replica cookie for explicit
                        # cleanup retry. Never log arbitrary server/error bodies.
                        raise ThalovantConnectionError(
                            "HiveMind HTTP disconnect was not acknowledged; admission is retained."
                        ) from None
                    self._admitted = False
                self._deadline = None
                if not self._closed:
                    self._session.close()
                    self._closed = True

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def on_mycroft(self, event: str, callback: Any) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def remove_mycroft(self, event: str, callback: Any) -> None:
        self._handlers[event] = [handler for handler in self._handlers.get(event, ()) if handler is not callback]

    def on(self, event: str, callback: Any) -> None:
        self._hive_handlers.setdefault(event, []).append(callback)

    def remove(self, event: str, callback: Any) -> None:
        self._hive_handlers[event] = [handler for handler in self._hive_handlers.get(event, ()) if handler is not callback]
