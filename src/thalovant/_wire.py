"""HiveMind envelopes, OVOS bus messages, and the WIRE-1 binary frame.

These used to come from hivemind-bus-client and ovos-bus-client. The SDK
needs little of either: the attributes a subscriber reads, JSON in both
directions, and the bit-level decoder for BINARY frames. The shapes follow
the libraries a hub runs, so a handler written against those still works.
"""

from __future__ import annotations

import copy
import json
import zlib
from typing import Any, Final

__all__ = [
    "BINARY_KINDS",
    "BusMessage",
    "HiveMessage",
    "binary_kind_name",
    "decode_binary_frame",
    "encode_binary_frame",
    "hive_message_from_json",
]

#: HiveMind message types by wire number (hivemind_bus_client.serialization).
_TYPE_BY_NUMBER: Final[dict[int, str]] = {
    0: "shake",
    1: "bus",
    2: "shared_bus",
    3: "broadcast",
    4: "propagate",
    5: "escalate",
    6: "hello",
    7: "query",
    8: "cascade",
    9: "ping",
    10: "rendezvous",
    11: "3rdparty",
    12: "bin",
}
_NUMBER_BY_TYPE: Final[dict[str, int]] = {
    **{name: number for number, name in _TYPE_BY_NUMBER.items()},
    "handshake": 0,
}

#: What a binary payload is, by the four-bit number the wire gives it.
BINARY_KINDS: Final[dict[int, str]] = {
    1: "raw_audio",
    2: "numpy_image",
    3: "file",
    4: "stt_transcribe",
    5: "stt_handle",
    6: "tts_audio",
}

#: Frame kinds whose payload is itself a whole HiveMind envelope.
_NESTED_KINDS: Final = frozenset(
    {"broadcast", "propagate", "escalate", "intercom", "rendezvous", "query", "cascade",
     "shared_bus"}
)


def binary_kind_name(number: int) -> str:
    """The name for a payload type, or ``binary:<n>`` for one nobody named."""
    return BINARY_KINDS.get(number, f"binary:{number}")


class BusMessage:
    """One OVOS bus message: a type, its data, and its context.

    Attribute-compatible with ``ovos_bus_client.message.Message`` for what a
    subscriber reads.
    """

    __slots__ = ("context", "data", "msg_type", "wire_context")

    def __init__(
        self,
        msg_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.msg_type = msg_type
        self.data: dict[str, Any] = data if isinstance(data, dict) else {}
        self.context: dict[str, Any] = context if isinstance(context, dict) else {}
        #: The context exactly as the hub sent it, before this node took the
        #: message in (see :func:`thalovant._hive.receive_bus`); ``None`` for
        #: a message that never crossed the wire. A reply is routed from it.
        self.wire_context: dict[str, Any] | None = None

    @property
    def as_dict(self) -> dict[str, Any]:
        return {"type": self.msg_type, "data": self.data, "context": self.context}

    def serialize(self) -> str:
        return json.dumps(self.as_dict, ensure_ascii=False)

    @classmethod
    def deserialize(cls, value: str | bytes | dict[str, Any]) -> BusMessage:
        raw = json.loads(value) if isinstance(value, (str, bytes)) else value
        if not isinstance(raw, dict):
            raise ValueError("A bus message must be a JSON object.")
        return cls(str(raw.get("type") or ""), raw.get("data"), raw.get("context"))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BusMessage):
            return NotImplemented
        return self.as_dict == other.as_dict

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"BusMessage({self.msg_type!r}, data={self.data!r}, context={self.context!r})"


class HiveMessage:
    """One HiveMind envelope.

    Attribute-compatible with ``hivemind_bus_client.message.HiveMessage`` for
    what an ``on_hive`` subscriber reads: ``msg_type`` compares equal to the
    type's name, a BUS payload is a :class:`BusMessage`, a mesh frame's payload
    is the inner :class:`HiveMessage`, and a BINARY frame's payload is bytes.
    """

    __slots__ = (
        "bin_type",
        "metadata",
        "msg_type",
        "node",
        "payload",
        "route",
        "source_peer",
        "target_pubkey",
        "target_site_id",
    )

    def __init__(
        self,
        msg_type: str,
        payload: Any = None,
        *,
        metadata: dict[str, Any] | None = None,
        route: list[Any] | None = None,
        node: Any = None,
        target_site_id: Any = None,
        target_pubkey: Any = None,
        source_peer: Any = None,
        bin_type: int = 0,
    ) -> None:
        self.msg_type = str(getattr(msg_type, "value", msg_type))
        self.payload = payload if payload is not None else {}
        self.metadata: dict[str, Any] = metadata if isinstance(metadata, dict) else {}
        self.route: list[Any] = route if isinstance(route, list) else []
        self.node = node
        self.target_site_id = target_site_id
        self.target_pubkey = target_pubkey
        self.source_peer = source_peer
        self.bin_type = bin_type

    @property
    def as_dict(self) -> dict[str, Any]:
        if self.msg_type == "bin":
            raise ValueError("A BINARY frame carries bytes and has no JSON form.")
        payload = self.payload
        if isinstance(payload, (HiveMessage, BusMessage)):
            payload = payload.as_dict
        return {
            "msg_type": self.msg_type,
            "payload": payload,
            "metadata": self.metadata,
            "route": self.route,
            "node": self.node,
            "target_site_id": self.target_site_id,
            "target_pubkey": self.target_pubkey,
            "source_peer": self.source_peer,
        }

    def serialize(self) -> str:
        return json.dumps(self.as_dict, ensure_ascii=False)

    @classmethod
    def deserialize(cls, value: str | bytes | dict[str, Any]) -> HiveMessage:
        return hive_message_from_json(value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, HiveMessage):
            return NotImplemented
        if self.msg_type == "bin" or other.msg_type == "bin":
            return (self.msg_type, self.payload, self.metadata, self.bin_type) == (
                other.msg_type, other.payload, other.metadata, other.bin_type,
            )
        return self.as_dict == other.as_dict

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"HiveMessage({self.msg_type!r}, payload={self.payload!r})"


def hive_message_from_json(value: str | bytes | dict[str, Any]) -> HiveMessage:
    """Parse one JSON HiveMind envelope, nested payloads included."""
    raw = json.loads(value) if isinstance(value, (str, bytes)) else copy.deepcopy(value)
    if not isinstance(raw, dict) or not isinstance(raw.get("msg_type"), str):
        raise ValueError("A HiveMind message is a JSON object with a msg_type.")
    msg_type = raw["msg_type"]
    payload: Any = raw.get("payload")
    if msg_type == "bus" and isinstance(payload, dict):
        payload = BusMessage(
            str(payload.get("type") or ""), payload.get("data"), payload.get("context")
        )
    elif msg_type in _NESTED_KINDS and isinstance(payload, dict) and isinstance(
        payload.get("msg_type"), str
    ):
        payload = hive_message_from_json(payload)
    route = raw.get("route")
    return HiveMessage(
        msg_type,
        payload if payload is not None else {},
        metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        route=route if isinstance(route, list) else [],
        node=raw.get("node"),
        target_site_id=raw.get("target_site_id"),
        target_pubkey=raw.get("target_pubkey"),
        source_peer=raw.get("source_peer"),
    )


class _BitReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    def bit(self) -> int:
        if self._offset >= len(self._payload) * 8:
            raise ValueError("unexpected end of HiveMind binary frame")
        value = (self._payload[self._offset // 8] >> (7 - self._offset % 8)) & 1
        self._offset += 1
        return value

    def uint(self, width: int) -> int:
        value = 0
        for _ in range(width):
            value = (value << 1) | self.bit()
        return value

    def take(self, length: int) -> bytes:
        start, shift = divmod(self._offset, 8)
        if shift == 0:
            end = start + length
            if end > len(self._payload):
                raise ValueError("unexpected end of HiveMind binary frame")
            self._offset += length * 8
            return self._payload[start:end]
        # Not byte-aligned -- a BINARY payload starts four bits into a byte.
        # Shift the whole span at once: a bit at a time is a second of CPU
        # for a spoken sentence.
        span = self._payload[start:start + length + 1]
        if len(span) < length + 1:
            raise ValueError("unexpected end of HiveMind binary frame")
        value = int.from_bytes(span, "big") >> (8 - shift)
        self._offset += length * 8
        return (value & ((1 << (length * 8)) - 1)).to_bytes(length, "big")

    def rest(self) -> bytes:
        return self.take((len(self._payload) * 8 - self._offset) // 8)

    def skip_padding(self) -> None:
        while self.bit() == 0:
            pass


#: The most a compressed part of a binary frame may inflate to. A reassembled
#: Noise message is itself capped at 32 MiB (``MAX_REASSEMBLY``); without a
#: cap here, a small frame of zeros from a hub could make the client allocate
#: gigabytes.
MAX_INFLATED = 32 * 1024 * 1024


def _inflate(payload: bytes) -> bytes:
    inflater = zlib.decompressobj()
    data = inflater.decompress(payload, MAX_INFLATED)
    if inflater.unconsumed_tail:
        raise ValueError("HiveMind binary frame inflates past the size limit")
    if not inflater.eof:
        # zlib.decompress refused a truncated stream; keep refusing it.
        raise ValueError("HiveMind binary frame holds a truncated compressed stream")
    return data


def _text(payload: bytes, compressed: bool) -> str:
    return (_inflate(payload) if compressed else payload).decode("utf-8")


def decode_binary_frame(frame: bytes) -> HiveMessage:
    """Decode one WIRE-1 bitstring frame.

    Left padding up to the first set bit, an optional version byte, a
    five-bit type, a compression flag, the metadata length and metadata, then
    either a JSON payload or -- for BINARY -- four bits naming the payload type
    and the raw bytes after them, never decompressed.
    """
    reader = _BitReader(bytes(frame))
    reader.skip_padding()
    if reader.bit() == 1:
        version = reader.uint(8)
        if version > 1:
            raise ValueError(f"unsupported HiveMind binary protocol version: {version}")
    type_number = reader.uint(5)
    compressed = reader.bit() == 1
    metadata_length = reader.uint(8)
    metadata = json.loads(_text(reader.take(metadata_length), compressed))
    if not isinstance(metadata, dict):
        raise ValueError("HiveMind binary metadata is not a JSON object")
    msg_type = _TYPE_BY_NUMBER.get(type_number, "3rdparty")
    if msg_type == "bin":
        kind = reader.uint(4)
        return HiveMessage("bin", reader.rest(), metadata=metadata, bin_type=kind)
    payload = json.loads(_text(reader.rest(), compressed))
    if not isinstance(payload, dict):
        raise ValueError("HiveMind binary payload is not a JSON object")
    return hive_message_from_json(
        {"msg_type": msg_type, "payload": payload, "metadata": metadata}
    )


def encode_binary_frame(message: HiveMessage) -> bytes:
    """Encode a JSON-carrying envelope the way the Go and Node SDKs do.

    Byte-aligned and uncompressed: a marker bit, the five-bit type and the
    compression bit fill the first byte; the metadata length the second.
    """
    type_number = _NUMBER_BY_TYPE.get(message.msg_type, 11)
    metadata = json.dumps(message.metadata, ensure_ascii=False).encode("utf-8")
    if len(metadata) > 255:
        raise ValueError("HiveMind binary metadata cannot exceed 255 bytes")
    body = message.as_dict["payload"]
    payload = json.dumps(body if isinstance(body, dict) else {}, ensure_ascii=False).encode()
    return bytes((0x80 | ((type_number & 0x1F) << 1), len(metadata))) + metadata + payload
