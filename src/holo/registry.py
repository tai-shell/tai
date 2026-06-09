"""Daemon-side registry mapping `sid` → `Channel`.

The WS server consults this on handshake to route an incoming socket to
the right Channel. Thread-safe so the WS server's per-connection thread
and the main thread (which calibrates new channels) can both touch it.

Single-process scope. Multi-daemon coordination is out of scope.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from holo.channel import Channel


class ChannelRegistry:
    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        self._lock = threading.Lock()

    def register(self, sid: str, channel: Channel) -> None:
        with self._lock:
            self._channels[sid] = channel

    def lookup(self, sid: str) -> Channel | None:
        with self._lock:
            return self._channels.get(sid)

    def unregister(self, sid: str) -> Channel | None:
        with self._lock:
            return self._channels.pop(sid, None)

    def items(self) -> list[tuple[str, Channel]]:
        """Snapshot the current sid → channel mapping.

        Returns a list rather than a view so the caller can iterate
        without holding the registry lock.
        """
        with self._lock:
            return list(self._channels.items())

    def __len__(self) -> int:
        with self._lock:
            return len(self._channels)
