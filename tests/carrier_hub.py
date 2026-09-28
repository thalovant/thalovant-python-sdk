"""A hub's Noise responder behind the HTTPS polling and MQTT carriers.

``fake_hub.FakeHub`` is the WebSocket hub. This is the same responder, with
no carrier of its own, so ``link-carrier-vectors.json`` can put each
handshake situation through HTTPS polling and through MQTT:

- :class:`CarrierPeer` answers the negotiation the way hivemind-core does:
  HELLO and the offer, the Noise responder (KK with the pinned client key,
  XX otherwise), the TOFU pin of the client's key, and an abort -- nothing
  sent, the session dropped -- on a first message it cannot read, a final
  message that does not authenticate, or a client key that contradicts the
  pin.
- :class:`HttpsHub` serves it over TLS the way hivemind-http-protocol does,
  and answers every request of an aborted session with 401, as the hub's
  listener refuses a session it no longer holds.
- :class:`MqttBroker` stands in for paho and a broker. MQTT has no refusal of
  its own to relay: an aborted session just stops answering.

Built on the SDK's own Noise, like ``fake_hub``.
"""

from __future__ import annotations

import base64
import json
import queue
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from thalovant import ThalovantIdentity, _noise

PASSWORD = "synthetic-carrier-password"


class CarrierPeer:
    """The hub side of one client's negotiation, whatever carries it."""

    def __init__(self, send: Callable[[str | bytes], None], node_id: str = "carrier-hub") -> None:
        self.send = send
        self.node_id = node_id
        self.password = PASSWORD
        self.static_key = _noise.generate_private_key()
        self.offer_kk = True
        self.pinned_client: bytes | None = None
        #: Flip a byte of the hub's KK answer, so it does not authenticate at the client.
        self.tamper_kk_answer = False
        self.patterns: list[str] = []
        self.aborted = False
        self.session: _noise.NoiseSession | None = None
        self._handshake: _noise.NoiseHandshake | None = None
        self._hello: dict[str, Any] = {}
        self._offer: dict[str, Any] = {}
        self._lock = threading.RLock()

    def begin(self) -> None:
        """A new session: HELLO and the offer, cleartext."""
        with self._lock:
            self.aborted = False
            self.session = None
            self._handshake = None
            patterns = [_noise.PATTERN_XX]
            if self.pinned_client is not None and self.offer_kk:
                patterns = [_noise.PATTERN_KK, _noise.PATTERN_XX]
            self._hello = {"node_id": self.node_id, "pubkey": "", "site_id": "hub"}
            self._offer = {
                "handshake": True, "min_protocol_version": 2, "max_protocol_version": 3,
                "binarize": False, "preshared_key": False, "password": True, "crypto_required": True,
                "encodings": ["JSON-HEX"], "ciphers": ["AES-GCM"],
                "noise": {"patterns": patterns, "suites": list(_noise.SUITES)},
            }
            self.send(json.dumps({"msg_type": "hello", "payload": self._hello}))
            self.send(json.dumps({"msg_type": "shake", "payload": self._offer}))

    def receive(self, raw: str | bytes) -> None:
        with self._lock:
            if self.aborted:
                return
            if self.session is not None:
                if isinstance(raw, bytes):
                    self.session.decrypt_frame(raw)
                return
            message = json.loads(raw)
            if message.get("msg_type") == "hello":
                return  # the client's cleartext HELLO over MQTT: already answered by begin()
            noise = message["payload"]["noise"]
            try:
                if "pattern" in noise:
                    self._first(noise)
                else:
                    assert self._handshake is not None
                    self._handshake.read_message(bytes.fromhex(noise["msg"]))
                    self._finish()
            except _noise.NoiseError:
                self._abort()

    def _first(self, noise: dict[str, Any]) -> None:
        pattern, suite = noise["pattern"], noise["suite"]
        self.patterns.append(pattern)
        prologue = _noise.build_prologue(self._hello, self._offer, _noise.protocol_name(pattern, suite))
        self._handshake = _noise.NoiseHandshake(
            pattern, suite, _noise.derive_psk(self.password, self.node_id), prologue, self.static_key,
            remote_static=self.pinned_client if pattern == _noise.PATTERN_KK else None, initiator=False,
        )
        self._handshake.read_message(bytes.fromhex(noise["msg"]))
        answer = bytearray(self._handshake.write_message(json.dumps({"encoding": "JSON-HEX"}).encode()))
        if self.tamper_kk_answer and pattern == _noise.PATTERN_KK:
            answer[-1] ^= 0x01
        self.send(json.dumps({"msg_type": "shake", "payload": {"noise": {"msg": answer.hex()}}}))
        if self._handshake.finished:
            self._finish()

    def _finish(self) -> None:
        assert self._handshake is not None
        session = self._handshake.into_session()
        client_key = session.remote_static
        if self.pinned_client is not None and client_key != self.pinned_client:
            self._abort()  # "client Noise static key contradicts pinned key"
            return
        self.pinned_client = client_key
        self.session = session

    def _abort(self) -> None:
        self.aborted = True
        self.session = None
        self._handshake = None


def _certificate(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "carrier-cert.pem", directory / "carrier-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    return cert_path, key_path


class HttpsHub:
    """hivemind-http-protocol's endpoints over TLS, answered by one :class:`CarrierPeer`."""

    def __init__(self, directory: Path) -> None:
        self.clear: queue.Queue[str] = queue.Queue()
        self.binary: queue.Queue[str] = queue.Queue()
        self.peer = CarrierPeer(self._queue)
        self.cert_path, key_path = _certificate(directory)
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_GET(self) -> None:
                hub._handle(self)

            def do_POST(self) -> None:
                hub._handle(self)

        self._server = ThreadingHTTPServer(("localhost", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert_path, key_path)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.endpoint = f"https://localhost:{self._server.server_port}"

    def _queue(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            self.binary.put(base64.b64encode(raw).decode("ascii"))
        else:
            self.clear.put(raw)

    @staticmethod
    def _drain(source: queue.Queue[str]) -> list[str]:
        items = []
        while not source.empty():
            items.append(source.get_nowait())
        return items

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        path = urlparse(request.path).path
        status, body = 200, {}
        cookie = None
        if path == "/connect":
            self._drain(self.clear)
            self._drain(self.binary)
            self.peer.begin()
            cookie = "hivemind_http_replica=one; Secure; HttpOnly; Path=/"
            body = {"status": "Connected"}
        elif path == "/disconnect":
            body = {"status": "Disconnected"}
        elif self.peer.aborted:
            status, body = 401, {"error": "Unauthorized"}
        elif path == "/get_messages":
            body = {"messages": self._drain(self.clear)}
        elif path == "/get_binary_messages":
            body = {"b64_messages": self._drain(self.binary)}
        elif path == "/send_message":
            form = parse_qs(request.rfile.read(int(request.headers.get("Content-Length", 0))).decode())
            raw: str | bytes = form["message"][0]
            if form.get("binary") == ["1"]:
                raw = base64.b64decode(raw, validate=True)
            self.peer.receive(raw)
            body = {"status": "message sent"}
        encoded = json.dumps(body).encode()
        request.send_response(status)
        if cookie:
            request.send_header("Set-Cookie", cookie)
        request.send_header("Content-Length", str(len(encoded)))
        request.end_headers()
        request.wfile.write(encoded)

    def identity(self) -> ThalovantIdentity:
        parsed = urlparse(self.endpoint)
        return ThalovantIdentity.from_mapping({
            "access_key": "synthetic-carrier-access", "password": PASSWORD, "site_id": "carrier",
            "default_master": f"https://{parsed.hostname}", "default_port": parsed.port,
        })

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class MqttBroker:
    """paho's client and a broker, in memory, in front of one :class:`CarrierPeer`."""

    def __init__(self) -> None:
        self.outbound: queue.Queue[str | bytes] = queue.Queue()
        self.peer = CarrierPeer(self.outbound.put)
        self.module = SimpleNamespace(Client=self._client, CallbackAPIVersion=SimpleNamespace(VERSION2=2))

    def _client(self, *_args: Any, **_options: Any) -> Any:
        broker = self

        class Published:
            def wait_for_publish(self, timeout: float | None = None) -> None:
                pass

            def is_published(self) -> bool:
                return True

        class Client:
            live = False
            on_connect: Any = None
            on_subscribe: Any = None
            on_disconnect: Any = None
            on_message: Any = None

            def __init__(self) -> None:
                self.stop = threading.Event()
                self.thread: threading.Thread | None = None

            def username_pw_set(self, *_: Any) -> None:
                pass

            def tls_set(self) -> None:
                pass

            def will_set(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def connect(self, *_args: Any, **_kwargs: Any) -> None:
                self.live = True

            def is_connected(self) -> bool:
                return self.live

            def loop_start(self) -> None:
                def run() -> None:
                    self.on_connect(self, None, None, 0)
                    while not self.stop.wait(0.005):
                        try:
                            raw = broker.outbound.get_nowait()
                        except queue.Empty:
                            continue
                        payload = raw.encode() if isinstance(raw, str) else raw
                        self.on_message(self, None, SimpleNamespace(topic="hivemind/carrier/out", payload=payload))

                self.thread = threading.Thread(target=run, daemon=True)
                self.thread.start()

            def loop_stop(self) -> None:
                self.stop.set()
                if self.thread is not None and self.thread is not threading.current_thread():
                    self.thread.join(timeout=2)

            def subscribe(self, *_args: Any, **_kwargs: Any) -> None:
                self.on_subscribe(self, None, 1, [1])

            def publish(self, topic: str, payload: Any, **_kwargs: Any) -> Published:
                if topic.endswith("/in"):
                    if isinstance(payload, (bytes, bytearray)) and broker.peer.session is not None:
                        broker.peer.receive(bytes(payload))  # transport frames are binary
                    else:
                        text = payload.decode() if isinstance(payload, (bytes, bytearray)) else payload
                        if json.loads(text).get("msg_type") == "hello":
                            broker.peer.begin()  # the cleartext HELLO opens a session
                        else:
                            broker.peer.receive(text)
                return Published()

            def disconnect(self) -> None:
                self.live = False
                broker.peer.session = None  # the hub forgets the session with the client
                self.on_disconnect(self, None, None, 0)

        return Client()

    def identity(self) -> ThalovantIdentity:
        return ThalovantIdentity.from_mapping({
            "access_key": "synthetic-carrier-access", "password": PASSWORD, "site_id": "carrier",
            "default_master": "https://broker.test", "default_port": 443,
            "mqtt": {"endpoint": "mqtts://broker.test:8883", "username": "synthetic-mqtt",
                     "password": "synthetic-broker-password", "tls": True, "topic_prefix": "hivemind/carrier"},
        })
