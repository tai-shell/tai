"""Single-process daemon that owns the WS server, registry, and channels.

Construct one `Daemon` per process; call `calibrate()` once per browser
tab. Each calibrated `Channel` is registered under its sid so the WS
server can route incoming handshakes to the right one. Channels for
which the WS handshake never lands stay on the QR/clipboard path
indefinitely — the WS poller is opportunistic, not load-bearing.

This sits above `Channel` so the existing test suite can keep
constructing bare `Channel()` instances for the QR-only path; only
production callers (`cli.py`, future SDK users) go through the daemon.

The Daemon also owns the SikuliX bridge — a single JVM subprocess
shared across all channels — gated by `enable_screen=True` (CLI
`--screen`). Started lazily on first access so tests and dev
environments without OpenJDK / the SikuliX jar fall through to the
legacy macOS-only input path in `_macos.py`.

Pass `no_bookmarklet=True` to skip the WS server entirely. That mode
suits agents that only drive screen / template / AppleScript tools
and never use the bookmarklet channel — the registry stays in place
but never gets populated, and `calibrate()` raises.
"""

from __future__ import annotations

from typing import Any

from holo.bridge import BridgeClient, BridgeError, BridgeMissingError
from holo.channel import Channel
from holo.registry import ChannelRegistry
from holo.ws_server import WSServer


class Daemon:
    def __init__(
        self,
        *,
        hide_qr: bool = False,
        enable_screen: bool = False,
        no_bookmarklet: bool = False,
        input_proxy: tuple[str, int] | None = None,
        remote_screen: tuple[str, int] | None = None,
    ) -> None:
        self.hide_qr = hide_qr
        self.enable_screen = enable_screen
        self.no_bookmarklet = no_bookmarklet
        self.registry = ChannelRegistry()
        if no_bookmarklet:
            self.ws_server: WSServer | None = None
        else:
            self.ws_server = WSServer(self.registry)
            self.ws_server.start()
        self._bridge: BridgeClient | None = None
        self._bridge_attempted: bool = False

        # Remote proxies: each routes a subset of bridge methods to a
        # `holo mcp --listen` on another host instead of the local
        # SikuliX bridge. `_remote_input` covers click/key/type/
        # scroll/activate; `_remote_screen` covers screenshot/
        # find_image/find_image_path/user_capture. Either can be unset
        # (None) → those ops use the local bridge.
        #
        # When both proxies target the same host:port, share one
        # backend instance (one MCP connection, one connect
        # subprocess). The set is small (`_backends` is keyed by
        # (host, port)) — only ever 1 or 2 entries.
        self._remote_input: Any = None
        self._remote_screen: Any = None
        self._backends: dict[tuple[str, int], Any] = {}
        if input_proxy is not None or remote_screen is not None:
            from holo.remote_backend import RemoteHoloBackend

            def _get(endpoint: tuple[str, int]) -> Any:
                if endpoint not in self._backends:
                    b = RemoteHoloBackend(endpoint[0], endpoint[1])
                    b.start()  # fail fast at boot if remote unreachable
                    self._backends[endpoint] = b
                return self._backends[endpoint]

            if input_proxy is not None:
                self._remote_input = _get(input_proxy)
            if remote_screen is not None:
                self._remote_screen = _get(remote_screen)

    @property
    def bridge(self) -> BridgeClient | None:
        """Lazy-start the SikuliX bridge if `enable_screen=True`.

        Returns the live `BridgeClient` on first success, `None` for
        every call when `enable_screen=False`, or `None` after a failed
        start (channels then fall through to the legacy `_macos.py`
        path). Opt-in keeps tests / dev environments without OpenJDK
        on the legacy path even when the jar happens to be present.
        """
        if not self.enable_screen:
            return None
        if self._bridge_attempted:
            return self._bridge
        self._bridge_attempted = True
        client = BridgeClient()
        try:
            client.start()
        except (BridgeMissingError, BridgeError, OSError):
            self._bridge = None
            return None
        self._bridge = client
        return self._bridge

    def calibrate(self, *, timeout: float | None = None) -> Channel:
        if self.no_bookmarklet:
            raise RuntimeError(
                "calibrate is disabled (--no-bookmarklet); the daemon was "
                "started without the WS server / bookmarklet channel"
            )
        ch = Channel(daemon=self, hide_qr=self.hide_qr)
        sid = ch.wait_for_calibration(timeout=timeout)
        self.registry.register(sid, ch)
        return ch

    def shutdown(self) -> None:
        if self.ws_server is not None:
            self.ws_server.stop()
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None
        # Stop each unique backend once (input + screen may share).
        for backend in self._backends.values():
            try:
                backend.stop()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
        self._backends.clear()
        self._remote_input = None
        self._remote_screen = None
