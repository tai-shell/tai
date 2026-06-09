"""Framing protocol for the holo channel.

Frames carry commands (daemon → page) and replies (page → daemon) over any
underlying transport — clipboard paste, document.title, or a same-origin
bridge socket. The wire format is identical regardless of transport, so the
two ends speak the same protocol whether they're co-located or paired
through a registry across the internet.

A frame is JSON with these fields:

    v        protocol version (currently 1)
    session  session id (uuid string), set at calibration
    type     "cmd" | "result" | "ack" | "ping" | "pong" | "bye"
    seq      0-indexed chunk number within this message
    total    total number of chunks in this message (>= 1)
    id       idempotency key (uuid string), shared across chunks of one message
    data     base64-encoded payload bytes (may be empty for ack/ping/bye)
    crc      crc32 of the decoded `data` bytes (8 hex chars)

Corruption is detected by JSON parse failure or CRC mismatch. Re-delivering
the same `id`+`seq` pair is idempotent — the receiver tracks delivered ids
and ignores replays. Page navigation is signalled by a "bye" frame so the
daemon can abort cleanly rather than typing into a dead document.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

PROTOCOL_VERSION = 1

FrameType = Literal["cmd", "result", "ack", "ping", "pong", "bye"]

_VALID_TYPES: frozenset[str] = frozenset(
    {"cmd", "result", "ack", "ping", "pong", "bye"}
)
_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"v", "session", "type", "seq", "total", "id", "data", "crc"}
)


class FrameError(Exception):
    """Raised when a frame fails to decode or validate."""


@dataclass(slots=True, frozen=True)
class Frame:
    session: str
    type: FrameType
    data: bytes = b""
    seq: int = 0
    total: int = 1
    id: str = field(default_factory=lambda: str(uuid4()))
    v: int = PROTOCOL_VERSION

    def encode(self) -> str:
        envelope = {
            "v": self.v,
            "session": self.session,
            "type": self.type,
            "seq": self.seq,
            "total": self.total,
            "id": self.id,
            "data": base64.b64encode(self.data).decode("ascii"),
            "crc": format(zlib.crc32(self.data), "08x"),
        }
        return json.dumps(envelope, separators=(",", ":"), sort_keys=True)


def decode(raw: str) -> Frame:
    """Parse and validate a wire-format frame string. Raises FrameError on any defect."""
    try:
        env = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FrameError(f"invalid json: {e}") from e

    if not isinstance(env, dict):
        raise FrameError("frame is not a json object")

    missing = _REQUIRED_FIELDS - env.keys()
    if missing:
        raise FrameError(f"missing fields: {sorted(missing)}")

    if env["v"] != PROTOCOL_VERSION:
        raise FrameError(f"unsupported version: {env['v']}")

    if env["type"] not in _VALID_TYPES:
        raise FrameError(f"unknown frame type: {env['type']}")

    try:
        data = base64.b64decode(env["data"], validate=True)
    except (ValueError, binascii.Error) as e:
        raise FrameError(f"invalid base64 data: {e}") from e

    actual_crc = format(zlib.crc32(data), "08x")
    if actual_crc != env["crc"]:
        raise FrameError(f"crc mismatch: expected {env['crc']}, got {actual_crc}")

    return Frame(
        session=env["session"],
        type=env["type"],
        data=data,
        seq=env["seq"],
        total=env["total"],
        id=env["id"],
        v=env["v"],
    )


def chunk(
    payload: bytes,
    *,
    session: str,
    type: FrameType,
    max_chunk: int = 32 * 1024,
) -> list[Frame]:
    """Split a payload into one or more frames sharing an id, with seq + total set."""
    if max_chunk <= 0:
        raise ValueError("max_chunk must be positive")
    if not payload:
        return [Frame(session=session, type=type, data=b"", total=1)]
    parts = [payload[i : i + max_chunk] for i in range(0, len(payload), max_chunk)]
    msg_id = str(uuid4())
    return [
        Frame(session=session, type=type, data=p, seq=i, total=len(parts), id=msg_id)
        for i, p in enumerate(parts)
    ]


class Reassembler:
    """Collects sequenced frames sharing an id; emits the joined payload when complete.

    Idempotent: re-feeding the same (id, seq) is a no-op once the message is
    complete, and duplicate chunks during reassembly are ignored. Callers
    should track returned non-None payloads as the authoritative delivery
    signal — repeats return None.
    """

    def __init__(self) -> None:
        self._buffers: dict[str, dict[int, bytes]] = {}
        self._totals: dict[str, int] = {}
        self._delivered: set[str] = set()

    def feed(self, frame: Frame) -> bytes | None:
        if frame.id in self._delivered:
            return None
        if not (0 <= frame.seq < frame.total):
            raise FrameError(
                f"seq {frame.seq} out of range for total {frame.total}"
            )
        prior_total = self._totals.get(frame.id)
        if prior_total is not None and prior_total != frame.total:
            raise FrameError(
                f"inconsistent total for id {frame.id}: "
                f"saw {prior_total}, now {frame.total}"
            )
        buf = self._buffers.setdefault(frame.id, {})
        if frame.seq in buf:
            return None
        buf[frame.seq] = frame.data
        self._totals[frame.id] = frame.total
        if len(buf) == frame.total:
            payload = b"".join(buf[i] for i in range(frame.total))
            self._delivered.add(frame.id)
            del self._buffers[frame.id]
            del self._totals[frame.id]
            return payload
        return None
