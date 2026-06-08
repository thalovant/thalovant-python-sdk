"""HiveMind data-plane transport adapters."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
import uuid
from typing import Any, Callable, NamedTuple, Protocol
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
                proactive_handshake_at = time.monotonic() + min(
                    1.0, transport.handshake_timeout
                )
                while time.monotonic() < deadline:
                    remaining = max(0.0, deadline - time.monotonic())
                    wait_for = min(transport.handshake_poll_interval, remaining)
                    if inner_self.handshake_event.wait(timeout=wait_for):
                        time.sleep(transport.handshake_settle_seconds)
                        return
                    should_start_handshake = (
                        inner_self.connected_event.is_set()
                        and not _runtime_crypto_key(transport.identity.crypto_key)
                        and time.monotonic() >= proactive_handshake_at
                    )
                    if should_start_handshake:
                        try:
                            inner_self.protocol.start_handshake()
                        except Exception as exc:
                            inner_self.thalovant_last_error = exc
                            transport._last_error = exc
                            raise
                    elif not inner_self.connected_event.is_set():
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


class MqttTopicSet(NamedTuple):
    c2s: str
    s2c: str
    status: str


class HiveMindMQTTTransport:
    """MQTT broker-mediated HiveMind transport following hivemind-mqtt-protocol."""

    def __init__(
        self,
        identity: ThalovantIdentity,
        *,
        useragent: str,
        connect_timeout: float = 4.0,
        handshake_timeout: float = 6.0,
        send_timeout: float = 8.0,
        **_: Any,
    ) -> None:
        self.identity = identity
        self.useragent = useragent
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.send_timeout = send_timeout
        self.session_id = f"thalovant-python-mqtt-{uuid.uuid4().hex}"
        self.topics = mqtt_topics_for_identity(identity)
        self._client: Any | None = None
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._handshake = threading.Event()
        self._last_error: BaseException | None = None
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self._crypto_key = _runtime_crypto_key(identity.crypto_key)
        self._cipher = "AES-GCM"
        self._json_encoding = "JSON-HEX"
        self._password_handshake: Any | None = None

    def connect(self) -> None:
        if self.is_connected():
            return
        if self.identity.mqtt is None:
            raise ThalovantConnectionError("The identity does not include MQTT broker credentials.")
        mqtt = self._load_mqtt_module()
        parsed = urlparse(self.identity.mqtt.endpoint)
        if parsed.scheme not in {"mqtt", "mqtts", "tcp", "ssl"} or not parsed.hostname:
            raise ThalovantConnectionError("MQTT endpoint must start with mqtt://, mqtts://, tcp://, or ssl://.")
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"thalovant-{_safe_mqtt_client_id(self.identity.access_key)}",
        )
        client.username_pw_set(self.identity.mqtt.username, self.identity.mqtt.password)
        if self.identity.mqtt.tls or parsed.scheme in {"mqtts", "ssl"}:
            client.tls_set()
        client.will_set(self.topics.status, "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        self._last_error = None
        client.connect(parsed.hostname, parsed.port or (8883 if parsed.scheme in {"mqtts", "ssl"} else 1883), keepalive=60)
        client.loop_start()
        if not self._connected.wait(timeout=self.connect_timeout):
            self.disconnect()
            raise ThalovantTimeoutError("HiveMind MQTT broker connection timed out.")
        self._subscribed.clear()
        client.subscribe(self.topics.s2c, qos=self.identity.mqtt.qos)
        if not self._subscribed.wait(timeout=self.connect_timeout):
            error = self.last_error()
            self.disconnect()
            detail = f": {error}" if error else ""
            raise ThalovantTimeoutError(f"HiveMind MQTT subscription timed out{detail}.")
        client.publish(self.topics.status, "online", qos=1, retain=True)
        self._send_hive_message(self._hello_message())
        if not self._handshake.wait(timeout=self.handshake_timeout):
            error = self.last_error()
            self.disconnect()
            detail = f": {error}" if error else ""
            raise ThalovantTimeoutError(f"HiveMind MQTT handshake timed out{detail}.")

    def disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.publish(self.topics.status, "offline", qos=1, retain=True)
            except Exception:
                pass
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        self._connected.clear()
        self._subscribed.clear()
        self._handshake.clear()

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._handlers.setdefault(event_name, []).append(handler)

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._handlers[event_name] = [
            entry for entry in self._handlers.get(event_name, []) if entry is not handler
        ]

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        message = {
            "msg_type": "bus",
            "payload": {
                "type": event_type,
                "data": data,
                "context": _runtime_bus_context(
                    context,
                    useragent=self.useragent,
                    session_id=self.session_id,
                    site_id=self.identity.site_id,
                ),
            },
            "metadata": {},
            "route": [],
            "node": None,
            "target_site_id": None,
            "target_pubkey": None,
            "source_peer": None,
        }
        return self._send_hive_message(message)

    def healthcheck(self) -> ThalovantHealth:
        return ThalovantHealth(
            connected=self._connected.is_set(),
            handshake_complete=self._handshake.is_set(),
            transport_alive=self.is_connected(),
            last_error=str(self._last_error) if self._last_error else None,
        )

    def is_connected(self) -> bool:
        client = self._client
        try:
            broker_connected = bool(client and client.is_connected())
        except Exception:
            broker_connected = False
        return self._connected.is_set() and self._handshake.is_set() and broker_connected

    def last_error(self) -> BaseException | None:
        return self._last_error

    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        if _reason_code_value(reason_code) != 0:
            self._last_error = ThalovantConnectionError(f"HiveMind MQTT connect failed: {reason_code}")
            return
        self._connected.set()

    def _on_subscribe(
        self,
        _client: Any,
        _userdata: Any,
        _mid: Any,
        reason_codes: Any,
        _properties: Any = None,
    ) -> None:
        codes = reason_codes if isinstance(reason_codes, (list, tuple)) else [reason_codes]
        failures = [code for code in codes if _reason_code_value(code) >= 128]
        if failures:
            self._last_error = ThalovantConnectionError(
                f"HiveMind MQTT subscribe failed: {failures[0]}"
            )
            return
        self._subscribed.set()

    def _on_disconnect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        if _reason_code_value(reason_code) != 0:
            self._last_error = ThalovantConnectionError(f"HiveMind MQTT disconnected: {reason_code}")
        self._connected.clear()
        self._subscribed.clear()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        try:
            self._handle_raw_message(message.payload)
        except Exception as exc:
            self._last_error = exc
            self._connected.clear()

    def _handle_raw_message(self, raw: bytes | str) -> None:
        message = self._decode_hive_message(raw)
        msg_type = _message_type_value(message.msg_type)
        payload = message.payload if isinstance(message.payload, dict) else {}
        if msg_type == "hello":
            return
        if msg_type in {"handshake", "shake"}:
            self._handle_handshake(payload)
        elif msg_type == "bus":
            bus_message = message.payload
            if hasattr(bus_message, "msg_type"):
                event_name = str(bus_message.msg_type)
                data = bus_message.data if isinstance(bus_message.data, dict) else {}
                context = bus_message.context if isinstance(bus_message.context, dict) else {}
            else:
                event_name = str(payload.get("type") or "")
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
            message = _RuntimeBusMessage(
                data=data,
                context=context,
                msg_type=event_name,
            )
            for handler in tuple(self._handlers.get(event_name, ())):
                handler(message)

    def _handle_handshake(self, payload: dict[str, Any]) -> None:
        self._select_mqtt_crypto(payload)
        if "envelope" in payload:
            if self._password_handshake is None:
                raise ThalovantConnectionError("HiveMind MQTT password handshake was not started.")
            self._password_handshake.receive_and_verify(payload["envelope"])
            self._crypto_key = self._password_handshake.secret
            self._send_hive_message(self._hello_message())
            self._handshake.set()
            return
        if payload.get("preshared_key") and not payload.get("handshake"):
            if not self._crypto_key:
                raise ThalovantConnectionError("HiveMind requested a preshared key, but identity.crypto_key is missing.")
            self._handshake.set()
            return
        if payload.get("password") and self.identity.password:
            from poorman_handshake import PasswordHandShake

            self._password_handshake = PasswordHandShake(self.identity.password)
            self._send_hive_message(
                {
                    "msg_type": "shake",
                    "payload": {
                        "binarize": False,
                        "encodings": ["JSON-HEX"],
                        "ciphers": ["AES-GCM"],
                        "envelope": self._password_handshake.generate_handshake(),
                    },
                    "metadata": {},
                    "route": [],
                    "node": None,
                    "target_site_id": None,
                    "target_pubkey": None,
                    "source_peer": None,
                }
            )
            return
        raise ThalovantConnectionError("Unsupported HiveMind MQTT handshake request.")

    def _send_hive_message(self, message: dict[str, Any]) -> Any:
        client = self._client
        if client is None or not client.is_connected():
            raise ThalovantConnectionError("HiveMind MQTT transport is not connected.")
        payload = self._encode_hive_message(message)
        result = client.publish(
            self.topics.c2s,
            payload,
            qos=self.identity.mqtt.qos if self.identity.mqtt else 1,
            retain=False,
        )
        result.wait_for_publish(timeout=self.send_timeout)
        return result

    def _hello_message(self) -> dict[str, Any]:
        return {
            "msg_type": "hello",
            "payload": {
                "pubkey": "",
                "session": {"session_id": self.session_id},
                "site_id": self.identity.site_id,
            },
            "metadata": {},
            "route": [],
            "node": None,
            "target_site_id": None,
            "target_pubkey": None,
            "source_peer": None,
        }

    def _encode_hive_message(self, message: dict[str, Any]) -> bytes:
        from hivemind_bus_client.client import get_bitstring
        from hivemind_bus_client.encryption import encrypt_bin
        from hivemind_bus_client.message import HiveMessage

        hive_message = HiveMessage(**message)
        bitstring = get_bitstring(
            hive_type=hive_message.msg_type,
            payload=hive_message.payload,
            compressed=False,
            hivemeta=hive_message.metadata,
            binary_type=hive_message.bin_type,
        ).bytes
        if self._crypto_key:
            return encrypt_bin(self._crypto_key, bitstring, cipher=self._cipher)
        return bitstring

    def _decode_hive_message(self, raw: bytes | str) -> Any:
        from hivemind_bus_client.client import decode_bitstring
        from hivemind_bus_client.encryption import decrypt_bin, decrypt_from_json
        from hivemind_bus_client.message import HiveMessage

        text = raw if isinstance(raw, str) else None
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None

        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and "ciphertext" in parsed:
                if not self._crypto_key:
                    raise ThalovantConnectionError("HiveMind MQTT encrypted payload requires a crypto key.")
                decrypted = decrypt_from_json(
                    self._crypto_key,
                    text,
                    cipher=self._cipher,
                    encoding=self._json_encoding,
                )
                return HiveMessage(**json.loads(decrypted))
            if isinstance(parsed, dict) and "msg_type" in parsed:
                return HiveMessage(**parsed)

        payload = raw.encode("utf-8") if isinstance(raw, str) else raw
        if self._crypto_key:
            payload = decrypt_bin(self._crypto_key, payload, cipher=self._cipher)
        return decode_bitstring(payload)

    def _select_mqtt_crypto(self, payload: dict[str, Any]) -> None:
        cipher = _enum_value(payload.get("cipher"))
        if cipher:
            self._cipher = cipher
        ciphers = [_enum_value(cipher) for cipher in payload.get("ciphers", [])]
        if "AES-GCM" in ciphers:
            self._cipher = "AES-GCM"
        encoding = _enum_value(payload.get("encoding"))
        if encoding:
            self._json_encoding = encoding
        encodings = [_enum_value(encoding) for encoding in payload.get("encodings", [])]
        if "JSON-HEX" in encodings:
            self._json_encoding = "JSON-HEX"

    @staticmethod
    def _load_mqtt_module() -> Any:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise ThalovantConnectionError("Install paho-mqtt before using MQTT runtime transport.") from exc
        return mqtt


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


@dataclass(frozen=True)
class _RuntimeBusMessage:
    data: dict[str, Any]
    context: dict[str, Any]
    msg_type: str


def mqtt_topics_for_identity(identity: ThalovantIdentity) -> MqttTopicSet:
    credentials = identity.mqtt
    if credentials is None:
        raise ThalovantConnectionError("The identity does not include MQTT broker credentials.")
    satellite_id = (
        hashlib.sha256(identity.access_key.encode("utf-8")).hexdigest()[:16]
        if credentials.hash_topics
        else identity.access_key
    )
    if credentials.c2s_topic and credentials.s2c_topic:
        return MqttTopicSet(
            credentials.c2s_topic,
            credentials.s2c_topic,
            credentials.status_topic or _sibling_mqtt_topic(credentials.c2s_topic, "status"),
        )
    raw = credentials.topic_prefix.strip("/") if credentials.topic_prefix else ""
    if raw:
        if "/c2s/" in raw:
            return MqttTopicSet(raw, _sibling_mqtt_topic(raw, "s2c"), _sibling_mqtt_topic(raw, "status"))
        if "/s2c/" in raw:
            return MqttTopicSet(_sibling_mqtt_topic(raw, "c2s"), raw, _sibling_mqtt_topic(raw, "status"))
        if "/status/" in raw:
            return MqttTopicSet(_sibling_mqtt_topic(raw, "c2s"), _sibling_mqtt_topic(raw, "s2c"), raw)
        parts = raw.split("/")
        base = "/".join(parts[:-1]) if parts[-1] in {identity.access_key, credentials.username, satellite_id} else raw
        hub_id = credentials.hub_id.strip("/") if credentials.hub_id else ""
        if hub_id and hub_id not in base.split("/"):
            base = f"{base}/{hub_id}"
    elif credentials.hub_id:
        base = f"hivemind/{credentials.hub_id.strip('/')}"
    else:
        raise ThalovantConnectionError("MQTT credentials must include topic_prefix, hub_id, or explicit c2s/s2c topics.")
    return MqttTopicSet(
        f"{base}/c2s/{satellite_id}",
        f"{base}/s2c/{satellite_id}",
        f"{base}/status/{satellite_id}",
    )


def _sibling_mqtt_topic(topic: str, segment: str) -> str:
    return re.sub(r"/(?:c2s|s2c|status)/", f"/{segment}/", topic, count=1)


def _safe_mqtt_client_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "-", value)[:48]
    return normalized or uuid.uuid4().hex


def _message_type_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw) if raw else ""


def _reason_code_value(reason_code: Any) -> int:
    try:
        return int(reason_code)
    except (TypeError, ValueError):
        value = getattr(reason_code, "value", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
