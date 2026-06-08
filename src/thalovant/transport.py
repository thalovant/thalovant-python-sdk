"""HiveMind data-plane transport adapters."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import time
from typing import Any, Callable, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .errors import (
    ThalovantConnectionError,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
)
from .events import _runtime_bus_context, _runtime_crypto_key
from .identity import ThalovantIdentity
from .models import ThalovantHealth


class Transport(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None: ...

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None: ...

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any: ...

    def healthcheck(self) -> ThalovantHealth: ...

    def is_connected(self) -> bool: ...

    def last_error(self) -> BaseException | None: ...


class HiveMindHTTPTransport:
    """Thin adapter around `hivemind_bus_client.http_client.HiveMindHTTPClient`."""

    def __init__(
        self,
        identity: ThalovantIdentity,
        *,
        useragent: str,
        connect_timeout: float = 4.0,
        handshake_timeout: float = 6.0,
        handshake_poll_interval: float = 0.1,
        handshake_settle_seconds: float = 0.1,
        send_timeout: float = 8.0,
        self_signed: bool = True,
        compress: bool = False,
        binarize: bool = False,
    ) -> None:
        self.identity = identity
        self.useragent = useragent
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.handshake_poll_interval = handshake_poll_interval
        self.handshake_settle_seconds = handshake_settle_seconds
        self.send_timeout = send_timeout
        self.self_signed = self_signed
        self.compress = compress
        self.binarize = binarize
        self._client: Any | None = None
        self._transport_connected = False
        self._deps: _HiveMindDeps | None = None
        self._last_error: BaseException | None = None

    def connect(self) -> None:
        if self.is_connected():
            return

        deps = self._load_deps()
        self._last_error = None
        http_client_class = self._build_http_client_class(deps.HiveMindHTTPClient)
        client = http_client_class(
            key=self.identity.access_key,
            password=self.identity.password,
            crypto_key=None,
            host=self.identity.default_master,
            port=self.identity.default_port,
            useragent=self.useragent,
            self_signed=self.self_signed,
            compress=self.compress,
            binarize=self.binarize,
        )
        protocol = self._build_protocol(client, deps)
        client.identity.site_id = self.identity.site_id
        client.protocol = protocol
        client.protocol.identity = client.identity
        client.protocol.site_id = self.identity.site_id
        client.protocol.bind(client.internal_bus)

        try:
            response = deps.requests.post(
                f"{client.base_url}/connect",
                params={"authorization": client.auth},
                timeout=self.connect_timeout,
            )
        except deps.requests.RequestException as exc:
            self._shutdown_client(client, transport_connected=False)
            raise ThalovantConnectionError("Could not reach the HiveMind HTTP endpoint.") from exc

        if getattr(response, "ok", False) is False:
            self._shutdown_client(client, transport_connected=False)
            detail = getattr(response, "text", "") or f"HTTP {getattr(response, 'status_code', 'error')}"
            raise ThalovantConnectionError(f"HiveMind HTTP connect failed: {detail}")

        client.connected.set()
        deadline = time.monotonic() + self.handshake_timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if client.handshake_event.wait(timeout=min(self.handshake_poll_interval, remaining)):
                time.sleep(self.handshake_settle_seconds)
                self._client = client
                self._transport_connected = True
                return

        self._shutdown_client(client, transport_connected=True)
        raise ThalovantTimeoutError("HiveMind HTTP handshake timed out.")

    def disconnect(self) -> None:
        client = self._client
        if client is None:
            return
        self._shutdown_client(client, transport_connected=self._transport_connected)
        self._client = None
        self._transport_connected = False

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._require_client().on_mycroft(event_name, handler)

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._require_client().remove_mycroft(event_name, handler)

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        deps = self._load_deps()
        client = self._require_live_client()
        message = deps.Message(
            event_type,
            data,
            _runtime_bus_context(
                context,
                useragent=client.useragent,
                session_id=client.session_id,
                site_id=client.site_id,
            ),
        )
        hive_message = deps.HiveMessage(deps.HiveMessageType.BUS, message)
        payload = deps.serialize_message(hive_message)
        if client.crypto_key:
            payload = deps.encrypt_as_json(
                client.crypto_key,
                payload,
                cipher=client.cipher,
                encoding=client.json_encoding,
            )

        try:
            response = deps.requests.post(
                f"{client.base_url}/send_message",
                data={"message": payload},
                params={"authorization": client.auth},
                timeout=self.send_timeout,
            )
        except deps.requests.RequestException as exc:
            self._last_error = exc
            raise ThalovantConnectionError("Could not send the HiveMind HTTP message.") from exc

        self._raise_for_emit_response(response)
        return response

    def healthcheck(self) -> ThalovantHealth:
        client = self._client
        connected = False
        handshake_complete = False
        transport_alive = False
        if client is not None and self._transport_connected:
            try:
                connected = bool(client.connected.is_set())
                handshake_complete = bool(client.handshake_event.is_set())
                transport_alive = bool(client.is_alive())
            except Exception as exc:
                self._last_error = exc
        error = self.last_error()
        return ThalovantHealth(
            connected=connected,
            handshake_complete=handshake_complete,
            transport_alive=transport_alive,
            last_error=str(error) if error else None,
        )

    def is_connected(self) -> bool:
        client = self._client
        if client is None or not self._transport_connected:
            return False
        try:
            return bool(
                client.connected.is_set()
                and client.handshake_event.is_set()
                and client.is_alive()
            )
        except Exception:
            return False

    def last_error(self) -> BaseException | None:
        client = self._client
        if client is not None:
            error = getattr(client, "thalovant_last_error", None)
            if isinstance(error, BaseException):
                return error
        return self._last_error

    def _require_client(self) -> Any:
        if self._client is None:
            raise ThalovantConnectionError("HiveMind HTTP transport is not connected.")
        return self._client

    def _require_live_client(self) -> Any:
        client = self._require_client()
        if not self.is_connected():
            error = self.last_error()
            detail = f": {error}" if error else ""
            raise ThalovantConnectionError(f"HiveMind HTTP transport is not connected{detail}")
        return client

    def _load_deps(self) -> "_HiveMindDeps":
        if self._deps is not None:
            return self._deps
        try:
            import requests
            from hivemind_bus_client.client import HiveMessageBusClient, WebSocketApp
            from hivemind_bus_client.encryption import encrypt_as_json
            from hivemind_bus_client.http_client import HiveMindHTTPClient
            from hivemind_bus_client.message import HiveMessage, HiveMessageType
            from hivemind_bus_client.protocol import HiveMindSlaveProtocol
            from hivemind_bus_client.util import serialize_message
            from ovos_bus_client.message import Message
            from ovos_bus_client.session import Session
        except ImportError as exc:
            raise ThalovantConnectionError(
                "Install the SDK with HiveMind dependencies before connecting."
            ) from exc

        self._deps = _HiveMindDeps(
            HiveMindHTTPClient=HiveMindHTTPClient,
            HiveMessageBusClient=HiveMessageBusClient,
            encrypt_as_json=encrypt_as_json,
            HiveMessage=HiveMessage,
            HiveMessageType=HiveMessageType,
            HiveMindSlaveProtocol=HiveMindSlaveProtocol,
            Message=Message,
            Session=Session,
            serialize_message=serialize_message,
            requests=requests,
            WebSocketApp=WebSocketApp,
        )
        return self._deps

    def _build_http_client_class(self, base_class: Any) -> Any:
        transport = self

        class _ObservedHiveMindHTTPClient(base_class):  # type: ignore[misc, valid-type]
            thalovant_last_error: BaseException | None = None

            @property
            def base_url(inner_self: Any) -> str:
                return transport.identity.endpoint_base()

            def run(inner_self: Any) -> None:
                try:
                    super().run()
                except Exception as exc:
                    inner_self.thalovant_last_error = exc
                    transport._last_error = exc
                    try:
                        inner_self.connected.clear()
                    except Exception:
                        pass

        return _ObservedHiveMindHTTPClient

    def _build_wss_client_class(self, base_class: Any, web_socket_app: Any) -> Any:
        transport = self

        class _ObservedHiveMessageBusClient(base_class):  # type: ignore[misc, valid-type]
            thalovant_last_error: BaseException | None = None

            def create_client(inner_self: Any) -> Any:
                return web_socket_app(
                    transport._authorized_wss_url(
                        key=inner_self.key,
                        useragent=inner_self.useragent,
                    ),
                    on_open=inner_self.on_open,
                    on_close=inner_self.on_close,
                    on_error=inner_self.on_error,
                    on_message=inner_self.on_message,
                )

            def run_forever(inner_self: Any) -> None:
                try:
                    super().run_forever()
                except Exception as exc:
                    inner_self.thalovant_last_error = exc
                    transport._last_error = exc
                    try:
                        inner_self.handshake_event.clear()
                    except Exception:
                        pass
                    raise

            def wait_for_handshake(
                inner_self: Any,
                timeout: float = 5,
                max_retries: int = 15,
            ) -> None:
                deadline = time.monotonic() + transport.handshake_timeout
                while time.monotonic() < deadline:
                    remaining = max(0.0, deadline - time.monotonic())
                    wait_for = min(transport.handshake_poll_interval, remaining)
                    if inner_self.handshake_event.wait(timeout=wait_for):
                        time.sleep(transport.handshake_settle_seconds)
                        return
                    if inner_self.connected_event.is_set():
                        try:
                            inner_self.protocol.start_handshake()
                        except Exception as exc:
                            inner_self.thalovant_last_error = exc
                            transport._last_error = exc
                            raise
                    else:
                        inner_self.connected_event.wait(timeout=wait_for)
                raise ThalovantTimeoutError("HiveMind WSS handshake timed out.")

        return _ObservedHiveMessageBusClient

    def _authorized_wss_url(self, *, key: str, useragent: str) -> str:
        endpoint = self.identity.endpoint_for("wss")
        if not endpoint:
            raise ThalovantConnectionError("The identity does not include a WSS endpoint.")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ThalovantConnectionError("WSS endpoint must start with ws:// or wss://.")
        authorization = base64.b64encode(f"{useragent}:{key}".encode("utf-8")).decode(
            "ascii"
        )
        query = [
            item
            for item in parse_qsl(parsed.query, keep_blank_values=True)
            if item[0] != "authorization"
        ]
        query.append(("authorization", authorization))
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path or "",
                "",
                urlencode(query),
                "",
            )
        )

    def _raise_for_emit_response(self, response: Any) -> None:
        status_code = getattr(response, "status_code", None)
        try:
            body = response.json()
        except Exception:
            body = {}

        error = body.get("error") if isinstance(body, dict) else None
        if error:
            if "not connected" in str(error).lower():
                exc = ThalovantConnectionError(f"HiveMind HTTP send failed: {error}")
                self._last_error = exc
                raise exc
            raise ThalovantRuntimeError(f"HiveMind HTTP send failed: {error}")

        if getattr(response, "ok", False) is False:
            detail = getattr(response, "text", "") or f"HTTP {status_code or 'error'}"
            raise ThalovantRuntimeError(f"HiveMind HTTP send failed: {detail}")

    def _build_protocol(self, client: Any, deps: "_HiveMindDeps") -> Any:
        crypto_key = _runtime_crypto_key(self.identity.crypto_key)

        if not crypto_key:
            return deps.HiveMindSlaveProtocol(
                client,
                shared_bus=client.share_bus,
                site_id=self.identity.site_id or "unknown",
                identity=client.identity,
            )

        class _ThalovantPresharedProtocol(deps.HiveMindSlaveProtocol):  # type: ignore[misc, valid-type]
            def handle_handshake(inner_self: Any, message: Any) -> None:
                payload = getattr(message, "payload", None)
                if (
                    isinstance(payload, dict)
                    and "envelope" not in payload
                    and payload.get("preshared_key")
                    and not payload.get("handshake")
                ):
                    inner_self.binarize = bool(payload.get("binarize", False))
                    session = deps.Session(inner_self.hm.session_id)
                    session.site_id = inner_self.site_id
                    inner_self.hm.emit(
                        deps.HiveMessage(
                            deps.HiveMessageType.HELLO,
                            {
                                "pubkey": inner_self.identity.public_key,
                                "session": session.serialize(),
                                "site_id": inner_self.site_id,
                            },
                        )
                    )
                    inner_self.hm.crypto_key = crypto_key
                    inner_self.hm.handshake_event.set()
                    return
                super().handle_handshake(message)

        return _ThalovantPresharedProtocol(
            client,
            shared_bus=client.share_bus,
            site_id=self.identity.site_id or "unknown",
            identity=client.identity,
        )

    def _shutdown_client(self, client: Any, *, transport_connected: bool) -> None:
        deps = self._load_deps()
        if transport_connected:
            try:
                deps.requests.post(
                    f"{client.base_url}/disconnect",
                    params={"authorization": client.auth},
                    timeout=self.connect_timeout,
                )
            except deps.requests.RequestException:
                pass
        try:
            client.connected.clear()
            client.handshake_event.clear()
        except Exception:
            pass
        try:
            client.connected.set()
            client.shutdown()
        except Exception:
            pass


class HiveMindWSSTransport(HiveMindHTTPTransport):
    """Adapter around `hivemind_bus_client.client.HiveMessageBusClient`."""

    def connect(self) -> None:
        if self.is_connected():
            return

        endpoint = self.identity.endpoint_for("wss")
        if not endpoint:
            raise ThalovantConnectionError("The identity does not include a WSS endpoint.")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ThalovantConnectionError("WSS endpoint must start with ws:// or wss://.")

        deps = self._load_deps()
        self._last_error = None
        wss_client_class = self._build_wss_client_class(
            deps.HiveMessageBusClient,
            deps.WebSocketApp,
        )
        client = wss_client_class(
            key=self.identity.access_key,
            password=self.identity.password,
            crypto_key=None,
            host=f"{parsed.scheme}://{_endpoint_host(parsed)}",
            port=parsed.port or (443 if parsed.scheme == "wss" else 80),
            useragent=self.useragent,
            self_signed=self.self_signed,
            compress=self.compress,
            binarize=self.binarize,
        )
        protocol = self._build_protocol(client, deps)

        try:
            client.connect(
                bus=client.internal_bus,
                protocol=protocol,
                site_id=self.identity.site_id,
            )
        except Exception as exc:
            self._last_error = exc
            self._shutdown_wss_client(client)
            if isinstance(exc, ThalovantTimeoutError):
                raise
            raise ThalovantConnectionError("HiveMind WSS connect failed.") from exc

        self._client = client
        self._transport_connected = True

    def disconnect(self) -> None:
        client = self._client
        if client is None:
            return
        self._shutdown_wss_client(client)
        self._client = None
        self._transport_connected = False

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._require_client().remove(event_name, handler)

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        deps = self._load_deps()
        client = self._require_live_client()
        message = deps.Message(
            event_type,
            data,
            _runtime_bus_context(
                context,
                useragent=client.useragent,
                session_id=client.session_id,
                site_id=client.site_id,
            ),
        )
        try:
            return client.emit_mycroft(message)
        except Exception as exc:
            self._last_error = exc
            raise ThalovantConnectionError("Could not send the HiveMind WSS message.") from exc

    def healthcheck(self) -> ThalovantHealth:
        client = self._client
        connected = False
        handshake_complete = False
        transport_alive = False
        if client is not None and self._transport_connected:
            try:
                connected = bool(client.connected_event.is_set())
                handshake_complete = bool(client.handshake_event.is_set())
                transport_alive = connected
            except Exception as exc:
                self._last_error = exc
        error = self.last_error()
        return ThalovantHealth(
            connected=connected,
            handshake_complete=handshake_complete,
            transport_alive=transport_alive,
            last_error=str(error) if error else None,
        )

    def is_connected(self) -> bool:
        client = self._client
        if client is None or not self._transport_connected:
            return False
        try:
            return bool(client.connected_event.is_set() and client.handshake_event.is_set())
        except Exception:
            return False

    def _require_client(self) -> Any:
        if self._client is None:
            raise ThalovantConnectionError("HiveMind WSS transport is not connected.")
        return self._client

    def _require_live_client(self) -> Any:
        client = self._require_client()
        if not self.is_connected():
            error = self.last_error()
            detail = f": {error}" if error else ""
            raise ThalovantConnectionError(f"HiveMind WSS transport is not connected{detail}")
        return client

    def _shutdown_wss_client(self, client: Any) -> None:
        try:
            client.handshake_event.clear()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
        try:
            client.connected_event.clear()
        except Exception:
            pass


@dataclass(frozen=True)
class _HiveMindDeps:
    HiveMindHTTPClient: Any
    HiveMessageBusClient: Any
    encrypt_as_json: Any
    HiveMessage: Any
    HiveMessageType: Any
    HiveMindSlaveProtocol: Any
    Message: Any
    Session: Any
    serialize_message: Any
    requests: Any
    WebSocketApp: Any


def _endpoint_host(parsed: Any) -> str:
    host = parsed.hostname or parsed.netloc
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host
