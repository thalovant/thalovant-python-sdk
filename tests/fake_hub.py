"""An in-process hub that speaks the real HiveMind v3 handshake over a WebSocket.

It follows HiveMind-core 5.x (``hivemind_core/protocol.py`` and
``hivemind_websocket_protocol``) step for step: the authorization query, the
cleartext HELLO and HANDSHAKE offer, the Noise responder, the TOFU pin of the
client's static key, the encrypted HELLO, and bus traffic after it. Where the
real hub closes a socket without a status (an unknown key, a Noise abort), so
does this one. It is built on the SDK's own Noise, checked separately against
``noiseprotocol`` and the reference vectors, so a bug there cannot hide here.

``HubThread`` runs one on a loop of its own, for the synchronous client's tests.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from thalovant import ThalovantIdentity, _noise
from thalovant.protocols import HubDataPlaneEndpoints, HubProtocolSettings

HUB_PEM = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0B\n-----END PUBLIC KEY-----"


@dataclass
class Registered:
    """A client record, as the hub's database holds it."""

    access_key: str
    password: str
    pinned_key: bytes | None = None


@dataclass
class Session:
    """One authenticated connection on the hub side."""

    ws: web.WebSocketResponse
    noise: _noise.NoiseSession
    pattern: str
    suite: str
    hello: dict[str, Any]
    useragent: str
    received: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)

    async def send_bus(
        self, msg_type: str, data: dict[str, Any], context: dict[str, Any] | None = None
    ) -> None:
        await self.send_hive({
            "msg_type": "bus",
            "payload": {"type": msg_type, "data": data, "context": context or {}},
            "metadata": {}, "route": [], "node": None,
            "target_site_id": None, "target_pubkey": None,
        })

    async def send_hive(self, message: dict[str, Any]) -> None:
        raw = json.dumps(message, ensure_ascii=False).encode("utf-8")
        for frame in self.noise.encrypt_message(raw):
            await self.ws.send_bytes(frame)

    async def send_binary(self, frame: bytes) -> None:
        for chunk in self.noise.encrypt_message(frame, is_json=False):
            await self.ws.send_bytes(chunk)


async def close_without_status(ws: web.WebSocketResponse) -> None:
    """What Tornado's ``close()`` puts on the wire: a close frame with no code."""
    assert ws._writer is not None
    await ws._writer.send_frame(b"", aiohttp.WSMsgType.CLOSE)


Responder = Callable[[Session, dict[str, Any]], Any]


class FakeHub:
    def __init__(self, node_id: str = "fake-hub-node") -> None:
        self.node_id = node_id
        self.static_key = _noise.generate_private_key()
        self.clients: dict[str, Registered] = {}
        self.suites: list[str] = list(_noise.SUITES)
        self.offer_kk = True
        self.admit = True
        self.silent = False
        self.close_after_handshake = False
        self.overloaded = False
        self.sessions: list[Session] = []
        self.attempts = 0
        self.patterns_chosen: list[str] = []
        self.new_session: asyncio.Queue[Session] = asyncio.Queue()
        #: Called with every bus message a client sends; may answer on the session.
        self.responder: Responder | None = None
        self._psk: dict[tuple[str, str], bytes] = {}
        self._server: TestServer | None = None
        self.url = ""
        self.port = 0

    def register(self, access_key: str = "hub-access", password: str = "hub-password") -> Registered:
        record = Registered(access_key, password)
        self.clients[access_key] = record
        return record

    def identity(self, record: Registered, *, site_id: str = "site") -> ThalovantIdentity:
        return ThalovantIdentity(
            access_key=record.access_key,
            password=record.password,
            site_id=site_id,
            default_master="https://127.0.0.1",
            default_port=self.port,
            data_plane_endpoints=HubDataPlaneEndpoints(wss=self.url),
            protocols=HubProtocolSettings(wss=True, http=False),
        )

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self.handler)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        self.port = self._server.port
        self.url = f"ws://127.0.0.1:{self.port}/"

    async def stop(self) -> None:
        await self.drop_all()
        if self._server is not None:
            await self._server.close()

    async def drop_all(self, *, code: int | None = None) -> None:
        for session in list(self.sessions):
            if code is None:
                await close_without_status(session.ws)
            await session.ws.close(code=code or aiohttp.WSCloseCode.GOING_AWAY)
        self.sessions.clear()

    def psk(self, password: str) -> bytes:
        key = (password, self.node_id)
        if key not in self._psk:
            self._psk[key] = _noise.derive_psk(password, self.node_id)
        return self._psk[key]

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        self.attempts += 1
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        try:
            useragent, key = self._decode(request.query.get("authorization", ""))
        except ValueError:
            await ws.close(code=1008, message=b"invalid authorization")
            return ws
        if self.overloaded:
            await ws.close(code=1013, message=b"authorization overloaded")
            return ws
        record = self.clients.get(key)
        if record is None or not self.admit:
            await close_without_status(ws)
            return ws
        if self.silent:
            # Accepted the socket, and never says HELLO.
            async for _ in ws:
                pass
            return ws

        hello = {"pubkey": HUB_PEM, "peer": f"{useragent}::{key[:6]}", "node_id": self.node_id}
        await ws.send_str(json.dumps(self._envelope("hello", hello)))
        patterns = [_noise.PATTERN_KK, _noise.PATTERN_XX]
        if record.pinned_key is None or not self.offer_kk:
            patterns = [_noise.PATTERN_XX]
        offer: dict[str, Any] = {
            "handshake": True, "min_protocol_version": 2, "max_protocol_version": 3,
            "binarize": False, "preshared_key": False, "password": True, "crypto_required": True,
            "encodings": ["JSON-B64", "JSON-HEX"], "ciphers": ["AES-GCM"],
            "noise": {"patterns": patterns, "suites": self.suites},
        }
        await ws.send_str(json.dumps(self._envelope("shake", offer)))

        session = await self._negotiate(ws, record, hello, offer, useragent)
        if session is None:
            return ws
        self.sessions.append(session)
        await self.new_session.put(session)
        async for message in ws:
            if message.type is not aiohttp.WSMsgType.BINARY:
                break
            frame = session.noise.decrypt_frame(message.data)
            if frame is None:
                continue
            decoded = json.loads(frame.payload)
            await session.received.put(decoded)
            if self.responder is not None:
                outcome = self.responder(session, decoded)
                if asyncio.iscoroutine(outcome):
                    await outcome
        if session in self.sessions:
            self.sessions.remove(session)
        return ws

    async def _negotiate(
        self, ws: web.WebSocketResponse, record: Registered, hello: dict[str, Any],
        offer: dict[str, Any], useragent: str,
    ) -> Session | None:
        first = await self._read_shake(ws)
        if first is None:
            return None
        pattern, suite = first.get("pattern"), first.get("suite")
        offered = offer.get("noise") or {}
        if pattern not in offered.get("patterns", []) or suite not in offered.get("suites", []):
            await close_without_status(ws)
            return None
        assert isinstance(pattern, str) and isinstance(suite, str)
        self.patterns_chosen.append(pattern)
        prologue = _noise.build_prologue(hello, offer, _noise.protocol_name(pattern, suite))
        handshake = _noise.NoiseHandshake(
            pattern, suite, self.psk(record.password), prologue, self.static_key,
            remote_static=record.pinned_key if pattern == _noise.PATTERN_KK else None,
            initiator=False,
        )
        try:
            node_payload = json.loads(handshake.read_message(bytes.fromhex(first["msg"])))
            assert node_payload == {"binarize": False, "encodings": []}
            response = handshake.write_message(json.dumps({"encoding": "JSON-HEX"}).encode())
        except _noise.NoiseError:
            await close_without_status(ws)
            return None
        await ws.send_str(json.dumps(self._envelope("shake", {"noise": {"msg": response.hex()}})))
        if not handshake.finished:
            final = await self._read_shake(ws)
            if final is None:
                return None
            try:
                handshake.read_message(bytes.fromhex(final["msg"]))
            except _noise.NoiseError:
                await close_without_status(ws)
                return None
        noise = handshake.into_session()
        client_key = noise.remote_static
        assert client_key is not None
        if record.pinned_key is not None and client_key != record.pinned_key:
            await close_without_status(ws)
            return None
        record.pinned_key = client_key

        message = await ws.receive()
        if message.type is not aiohttp.WSMsgType.BINARY:
            return None
        frame = noise.decrypt_frame(message.data)
        assert frame is not None and frame.is_json
        client_hello = json.loads(frame.payload)
        assert client_hello["msg_type"] == "hello"
        if client_hello["payload"]["session"]["session_id"] == "default":
            await close_without_status(ws)
            return None
        if self.close_after_handshake:
            await close_without_status(ws)
            return None
        return Session(ws, noise, pattern, suite, client_hello["payload"], useragent)

    async def _read_shake(self, ws: web.WebSocketResponse) -> dict[str, Any] | None:
        message = await ws.receive()
        if message.type is not aiohttp.WSMsgType.TEXT:
            return None
        frame = json.loads(message.data)
        assert frame["msg_type"] == "shake"
        noise = frame["payload"]["noise"]
        assert isinstance(noise, dict)
        return noise

    @staticmethod
    def _decode(auth: str) -> tuple[str, str]:
        try:
            decoded = base64.b64decode(auth.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError("invalid authorization encoding") from exc
        name, _, key = decoded.partition(":")
        if not name or not key:
            raise ValueError("invalid authorization payload")
        return name, key

    @staticmethod
    def _envelope(msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "msg_type": msg_type, "payload": payload, "metadata": {}, "route": [],
            "node": None, "target_site_id": None, "target_pubkey": None,
        }


def speak_back(session: Session, message: dict[str, Any]) -> Any:
    """A responder that answers an utterance the way ovos-core does: speak, then handled."""
    if message.get("msg_type") != "bus":
        return None
    payload = message["payload"]
    if payload.get("type") != "recognizer_loop:utterance":
        return None
    context = dict(payload.get("context") or {})
    utterance = (payload.get("data") or {}).get("utterances", [""])[0]

    async def answer() -> None:
        await session.send_bus("speak", {"utterance": f"You said {utterance}"}, context)
        await session.send_bus("ovos.utterance.handled", {}, context)

    return answer()


class HubThread:
    """A :class:`FakeHub` on a loop of its own, driven from synchronous tests."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="fake-hub")
        self.thread.start()
        self.hub = self.call(self._make)

    async def _make(self) -> FakeHub:
        hub = FakeHub()
        await hub.start()
        return hub

    def call(self, coro_fn: Callable[..., Any], *args: Any, timeout: float = 10) -> Any:
        future: concurrent.futures.Future[Any] = asyncio.run_coroutine_threadsafe(
            coro_fn(*args), self.loop
        )
        return future.result(timeout)

    def session(self, timeout: float = 5) -> Session:
        return self.call(lambda: asyncio.wait_for(self.hub.new_session.get(), timeout))

    def close(self) -> None:
        try:
            self.call(self.hub.stop)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
