"""Loopback HTTP + WebSocket server — Phase 1 transport.

The bookmarklet's first command after calibration is a `ws_handshake`
op delivered via the clipboard channel. That op carries this server's
popup URL + per-process token. The bookmarklet's about:blank popup
navigates itself to the popup URL — the popup is now loaded from the
daemon's loopback origin and has its own (permissive) CSP, so it can
open a `WebSocket` back to the daemon on any host page, no matter how
strict the host's `connect-src` / `default-src` is. The first WS
message is `{type: "handshake", sid, token}` and the server attaches
the connection to the matching Channel via the registry.

After handshake, the wire protocol on each connection is:

    daemon → page  {"type": "cmd",    "frame": <wire-format frame string>}
    page   → daemon {"type": "result", "frame": <wire-format frame string>}

Frames carry the same envelope as the clipboard/QR path, so command
dispatch and reply matching reuse `holo.framing` unchanged. Per-process
token + per-channel sid binding mean only the page the daemon actually
locked onto can use the listener.

Runs on a daemon thread; the websockets sync server uses one thread per
connection internally. Channel.send_command is sync and calls
`ws.send` directly from its own thread (the websockets sync API
serializes its internal I/O so this is safe).
"""

from __future__ import annotations

import http
import json
import logging
import secrets
import threading
from importlib import resources
from typing import TYPE_CHECKING

from websockets.http11 import Response
from websockets.sync.server import ServerConnection, serve

if TYPE_CHECKING:
    from holo.registry import ChannelRegistry


HANDSHAKE_TIMEOUT_S: float = 5.0
START_TIMEOUT_S: float = 5.0
POPUP_PATH: str = "/popup.html"
FRAMING_PATH: str = "/framing.js"

_STATIC_FILES: dict[str, tuple[str, str]] = {
    POPUP_PATH: ("popup.html", "text/html; charset=utf-8"),
    FRAMING_PATH: ("framing.js", "application/javascript; charset=utf-8"),
}


def _load_static(name: str) -> str:
    return resources.files("holo.static").joinpath(name).read_text(encoding="utf-8")


class WSServer:
    """Background HTTP + WebSocket server bound to 127.0.0.1 on a random port."""

    def __init__(self, registry: ChannelRegistry, *, host: str = "127.0.0.1") -> None:
        self.registry = registry
        self.host = host
        self.token = secrets.token_urlsafe(24)
        # Read the static files at construction so a packaging error
        # surfaces here rather than on first popup load.
        self._static_bodies: dict[str, tuple[str, str]] = {
            path: (_load_static(name), ctype)
            for path, (name, ctype) in _STATIC_FILES.items()
        }
        self._port: int | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("WS server not started")
        return self._port

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/"

    @property
    def popup_url(self) -> str:
        return f"http://{self.host}:{self.port}{POPUP_PATH}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="holo-ws")
        self._thread.start()
        if not self._ready.wait(timeout=START_TIMEOUT_S):
            raise RuntimeError(f"WS server did not start within {START_TIMEOUT_S}s")
        if self._start_error is not None:
            raise self._start_error

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def _run(self) -> None:
        # The websockets library logs every non-WS request as
        # "opening handshake failed" at ERROR level — noisy and
        # misleading since serving popup.html / framing.js as plain
        # HTTP is intentional. Mute that one logger.
        logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
        try:
            with serve(
                self._handler,
                self.host,
                0,
                process_request=self._process_request,
            ) as server:
                self._server = server
                self._port = server.socket.getsockname()[1]
                self._ready.set()
                server.serve_forever()
        except BaseException as e:  # noqa: BLE001 — surface any startup failure
            self._start_error = e
            self._ready.set()

    def _process_request(self, conn: ServerConnection, request) -> Response | None:
        """Serve static files for plain HTTP requests; pass through for WS upgrades.

        Path-based routing on the same port. `/popup.html` is the
        cross-origin popup body (loaded after the bookmarklet's
        about:blank popup navigates to escape the host page's CSP);
        `/framing.js` is the wire-format codec the popup imports.
        Anything else falls through to the WS upgrade path; non-
        handshake WS traffic is rejected by `_handshake`.
        """
        entry = self._static_bodies.get(request.path)
        if entry is None:
            return None
        body, ctype = entry
        response = conn.respond(http.HTTPStatus.OK, body)
        # `respond()` seeds Content-Type: text/plain. Headers is a
        # multi-dict, so __setitem__ would *append* — clear first.
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = ctype
        response.headers["Cache-Control"] = "no-store"
        return response

    def _handler(self, ws: ServerConnection) -> None:
        ch = self._handshake(ws)
        if ch is None:
            return
        try:
            ws.send(json.dumps({"type": "handshake_ack"}))
            ch._on_ws_attached(ws)
            for raw in ws:
                ch._on_ws_message(raw)
        finally:
            ch._on_ws_detached()

    def _handshake(self, ws: ServerConnection):
        try:
            raw = ws.recv(timeout=HANDSHAKE_TIMEOUT_S)
        except TimeoutError:
            ws.close(code=1008, reason="handshake timeout")
            return None
        try:
            msg = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            msg = None
        if not isinstance(msg, dict):
            ws.close(code=1008, reason="bad json")
            return None
        if msg.get("type") != "handshake":
            ws.close(code=1008, reason="expected handshake")
            return None
        if msg.get("token") != self.token:
            ws.close(code=1008, reason="bad token")
            return None
        sid = msg.get("sid")
        if not isinstance(sid, str):
            ws.close(code=1008, reason="bad sid")
            return None
        ch = self.registry.lookup(sid)
        if ch is None:
            ws.close(code=1008, reason="unknown sid")
            return None
        return ch
