"""Real TLS HTTP and MQTT carrier regressions against an r8-shaped Noise peer."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import ssl
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from hivemind_bus_client.noise import NoiseTransport, build_prologue, canonical_json, noise_protocol_name, start_noise_handshake

from thalovant import ThalovantIdentity, ThalovantConnectionError
from thalovant.transport import HiveMindHTTPTransport, HiveMindMQTTTransport, HiveMindWSSTransport

PASSWORD = "synthetic-conformance-password"


def identity(endpoint="https://localhost:443"):
    parsed = urlparse(endpoint)
    return ThalovantIdentity.from_mapping({
        "access_key": "synthetic-access", "password": PASSWORD, "site_id": "conformance",
        "default_master": f"https://{parsed.hostname}", "default_port": parsed.port,
        "mqtt": {"endpoint": "mqtts://broker.test:8883", "username": "synthetic-mqtt", "password": "synthetic-broker-password",
                 "tls": True, "topic_prefix": "hivemind/test/client"},
    })


class Peer:
    def __init__(self, root, send):
        self.root, self.send = root, send
        self.pin = None
        self.patterns = []
        self.received = []
        self.session = None
        self.hello = {"node_id": "conformance-node", "pubkey": "", "site_id": "hub"}
        self.offer = {"max_protocol_version": 3, "binarize": False, "encodings": ["JSON-HEX"], "ciphers": ["AES-GCM"],
                      "noise": {"patterns": ["KKpsk0", "XXpsk2"], "suites": ["25519_ChaChaPoly_SHA256", "25519_AESGCM_SHA256"]}}

    def start(self):
        self.session = None
        self.hs = None
        for kind, payload in (("hello", self.hello), ("shake", self.offer)):
            self.send(json.dumps({"msg_type": kind, "payload": payload}))

    def receive(self, raw):
        if self.session is not None:
            assert isinstance(raw, bytes), "application traffic must be encrypted binary"
            plain = self.session.decrypt_frame(raw)
            if plain is None:
                return
            message = json.loads(plain)
            self.received.append(message)
            if message["msg_type"] == "bus":
                payload = message["payload"]
                self.session.send_message(json.dumps({"msg_type": "bus", "payload": {
                    "type": "speak", "data": {"utterance": "reply", "large": payload["data"].get("large", "")},
                    "context": payload["context"]}}), self.send)
            return
        message = json.loads(raw)
        if message["msg_type"] == "hello":
            self.start()
            return
        assert message["msg_type"] == "shake"
        params = message["payload"]["noise"]
        if "pattern" in params:
            self.patterns.append(params["pattern"])
            name = noise_protocol_name(params["pattern"], params["suite"])
            self.hs = start_noise_handshake(False, params["pattern"], params["suite"], PASSWORD, self.hello["node_id"],
                    build_prologue(self.hello, self.offer, name), key_path=str(self.root / "server.key"), remote_pubkey=self.pin)
            self.hs.read_message(bytes.fromhex(params["msg"]))
            response = self.hs.write_message(canonical_json({"binarize": False}))
            self.send(json.dumps({"msg_type": "shake", "payload": {"noise": {"msg": response.hex()}}}))
        else:
            self.hs.read_message(bytes.fromhex(params["msg"]))
        if self.hs.handshake_finished:
            self.session = NoiseTransport(self.hs)
            self.pin = self.session.remote_static_key


@pytest.fixture
def http_peer(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "tls.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert_path))
    clear, binary = queue.Queue(), queue.Queue()
    def send(raw):
        (binary if isinstance(raw, bytes) else clear).put(base64.b64encode(raw).decode() if isinstance(raw, bytes) else raw)
    peer = Peer(tmp_path, send)
    peer.error = False
    peer.admitted = False
    peer.fail_poll = False
    peer.cookies = 0
    def drain(q):
        output = []
        while not q.empty():
            output.append(q.get_nowait())
        return output
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self): self.handle_request()
        def do_POST(self): self.handle_request()
        def handle_request(self):
            path = urlparse(self.path).path
            cookie = None
            try:
                if path == "/connect":
                    if not peer.admitted and not peer.error:
                        drain(clear); drain(binary); peer.start()
                        peer.admitted = True
                    cookie = "hivemind_http_replica=one; Secure; HttpOnly; Path=/"
                    body = {"error": "denied"} if peer.error else {"status": "Connected"}
                else:
                    assert self.headers.get("Cookie") == "hivemind_http_replica=one"
                    peer.cookies += 1
                    if path == "/get_messages":
                        body = {"error": "transient poll failure"} if peer.fail_poll else {"messages": drain(clear)}
                        peer.fail_poll = False
                    elif path == "/get_binary_messages": body = {"b64_messages": drain(binary)}
                    elif path == "/disconnect":
                        peer.admitted = False
                        body = {"status": "Disconnected"}
                    else:
                        assert path == "/send_message"
                        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
                        raw = form["message"][0]
                        if form.get("binary") == ["1"]: raw = base64.b64decode(raw, validate=True)
                        peer.receive(raw)
                        body = {"status": "message sent"}
            except Exception as exc:
                body = {"error": type(exc).__name__}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            if cookie: self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
    server = ThreadingHTTPServer(("localhost", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert_path, key_path)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield peer, f"https://localhost:{server.server_port}"
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_https_noise_cookie_encrypted_chunked_reply_and_same_object_reconnect(http_peer, tmp_path):
    peer, endpoint = http_peer
    transport = HiveMindHTTPTransport(identity(endpoint), useragent="conformance", noise_state_dir=str(tmp_path / "client"), handshake_poll_interval=0.01)
    try:
        for attempt in range(2):
            transport.connect()
            assert transport.healthcheck().ok
            replies = []
            transport.on_mycroft("speak", replies.append)
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda n: transport.emit_event("ovos.intent.list", {"large": "x" * 140000}, {"request_id": f"{attempt}-{n}"}), range(3)))
            deadline = time.monotonic() + 4
            while len(replies) < 3 and time.monotonic() < deadline: time.sleep(0.01)
            assert sorted(reply.context["request_id"] for reply in replies) == [f"{attempt}-{n}" for n in range(3)]
            assert all(len(reply.data["large"]) == 140000 for reply in replies)
            transport.disconnect()
            assert not transport.healthcheck().handshake_complete
        assert peer.patterns == ["XXpsk2", "KKpsk0"]
        assert peer.cookies > 10
    finally: transport.disconnect()


@pytest.mark.parametrize("failure", ["legacy", "password", "json-error", "untrusted-tls"])
def test_https_refuses_unusable_session(http_peer, tmp_path, monkeypatch, failure):
    peer, endpoint = http_peer
    ident = identity(endpoint)
    if failure == "legacy": peer.offer = {"preshared_key": True}
    elif failure == "password": ident = replace(ident, password="wrong-password")
    elif failure == "json-error": peer.error = True
    else: monkeypatch.delenv("REQUESTS_CA_BUNDLE")
    transport = HiveMindHTTPTransport(ident, useragent="conformance", noise_state_dir=str(tmp_path / "client"), handshake_timeout=2)
    with pytest.raises(ThalovantConnectionError): transport.connect()
    assert not transport.healthcheck().connected
    assert not transport.healthcheck().handshake_complete


def test_python_tls_is_verified_by_default():
    assert HiveMindHTTPTransport(identity(), useragent="test").self_signed is False
    assert HiveMindWSSTransport(identity(), useragent="test").self_signed is False
    assert HiveMindWSSTransport(identity(), useragent="test", self_signed=True).self_signed is True


class PublishResult:
    def wait_for_publish(self, timeout=None): pass
    def is_published(self): return True


class Broker:
    def __init__(self, root):
        self.root = root
        self.client = None
        self.incoming = queue.Queue()
        self.peer = Peer(root, self.incoming.put)
    def Client(self, *_args, **options):
        assert options["reconnect_on_failure"] is False
        assert "synthetic-access" not in options["client_id"]
        broker = self
        class Client:
            live = False
            stop = threading.Event()
            def username_pw_set(self, *_): pass
            def tls_set(self): self.tls = True
            def will_set(self, *_args, **_kwargs): pass
            def connect(self, *_args, **_kwargs): self.live = True
            def is_connected(self): return self.live
            def loop_start(self):
                def run():
                    self.on_connect(self, None, None, 0)
                    while not self.stop.wait(0.005):
                        try: raw = broker.incoming.get_nowait()
                        except queue.Empty: continue
                        self.on_message(self, None, SimpleNamespace(topic="hivemind/test/client/out", payload=raw.encode() if isinstance(raw, str) else raw))
                self.thread = threading.Thread(target=run, daemon=True); self.thread.start()
            def loop_stop(self):
                self.stop.set()
                if self.thread is not threading.current_thread(): self.thread.join(timeout=2)
            def subscribe(self, *_args, **_kwargs): self.on_subscribe(self, None, 1, [1])
            def publish(self, topic, payload, **_kwargs):
                if topic.endswith("/in"): broker.peer.receive(payload)
                return PublishResult()
            def disconnect(self):
                self.live = False
                broker.peer.session = None
                self.on_disconnect(self, None, None, 0)
        self.client = Client()
        return self.client


def test_mqtt_noise_raw_frames_threaded_replies_reconnect_and_pin_preservation(tmp_path, monkeypatch):
    broker = Broker(tmp_path)
    module = SimpleNamespace(Client=broker.Client, CallbackAPIVersion=SimpleNamespace(VERSION2=2))
    transport = HiveMindMQTTTransport(identity(), useragent="conformance", noise_state_dir=str(tmp_path / "client"))
    monkeypatch.setattr(transport, "_load_mqtt_module", lambda: module)
    replies = []
    transport.on_mycroft("speak", replies.append)
    try:
        for attempt in range(2):
            transport.connect()
            assert broker.client.tls
            assert transport.healthcheck().ok
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda n: transport.emit_event("ovos.intent.list", {"large": "x" * 140000}, {"request_id": f"{attempt}-{n}"}), range(3)))
            deadline = time.monotonic() + 3
            while len(replies) < (attempt + 1) * 3 and time.monotonic() < deadline: time.sleep(0.01)
            assert len(replies) == (attempt + 1) * 3
            if attempt == 0: transport.disconnect()
        assert broker.peer.patterns == ["XXpsk2", "KKpsk0"]
        store = transport._noise.store
        pin = store.get_pinned_noise_key(transport._noise.pin_id)
        broker.incoming.put(b"tampered")
        deadline = time.monotonic() + 2
        while transport.healthcheck().handshake_complete and time.monotonic() < deadline: time.sleep(0.01)
        assert not transport.healthcheck().handshake_complete
        assert store.get_pinned_noise_key(transport._noise.pin_id) == pin
    finally: transport.disconnect()


def test_https_poll_failure_reconnect_clears_previous_admission(http_peer, tmp_path):
    peer, endpoint = http_peer
    transport = HiveMindHTTPTransport(identity(endpoint), useragent="conformance", noise_state_dir=str(tmp_path / "client"), handshake_poll_interval=0.01)
    try:
        transport.connect()
        peer.fail_poll = True
        deadline = time.monotonic() + 2
        while transport.healthcheck().connected and time.monotonic() < deadline: time.sleep(0.01)
        assert not transport.healthcheck().connected
        assert peer.admitted, "a polling failure does not unregister the server session"
        transport.connect()
        assert transport.healthcheck().ok
        assert peer.patterns == ["XXpsk2", "KKpsk0"]
    finally: transport.disconnect()
