"""Daemon-side channel coordinator.

Composes the OS primitives — `holo.windows.list_windows()` for the
page → daemon direction, `holo.clipboard.paste()` for daemon → page,
and `holo.framing` for the wire format — into a single `Channel`
class with a small, blocking, request/response surface:

    ch = Channel()
    sid = ch.wait_for_calibration()       # picks up the bookmarklet beacon
    result = ch.send_command({"op": "ping"})

The Channel locks onto a specific browser window at calibration and
polls only that window's title for replies. Multi-window / multi-tab
addressing is deferred — first user to actually need it owns the
design.

Two transports for command/result traffic, in order of preference:

* WebSocket — opportunistically established on the first `send_command`
  via a `ws_handshake` op pasted through the clipboard. Once the page
  has connected back to the daemon's loopback `WSServer`, subsequent
  commands skip the focus-stealing paste and ride the socket. Requires
  a `Daemon` to own the channel (see `holo.daemon.Daemon`).
* Clipboard + QR (the Phase 0 path) — kept as a hot fallback whenever
  the WS handshake has not landed yet, the WS connection drops, or the
  Channel was constructed standalone (no `Daemon` attached).

Single-frame replies only in this layer. Multi-frame reassembly is
implemented in `holo.framing.Reassembler` and will be wired into the
channel when a Phase 1 caller actually needs it; today's commands
(`ping`, `read_global` for short values) all fit in one title.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from holo import clipboard, framing
from holo import title as title_mod
from holo.windows import WindowInfo, list_windows

if TYPE_CHECKING:
    from holo.daemon import Daemon

# Common browser owner-name strings on macOS. Users with other browsers
# can extend this via the `browsers` constructor arg.
DEFAULT_BROWSERS: frozenset[str] = frozenset(
    {
        "Google Chrome",
        "Google Chrome Canary",
        "Firefox",
        "Firefox Developer Edition",
        "Safari",
        "Brave Browser",
        "Microsoft Edge",
        "Arc",
        "Vivaldi",
        "Chromium",
    }
)

DEFAULT_POLL_INTERVAL_S: float = 0.05
DEFAULT_TIMEOUT_S: float = 5.0

# Sentinel for `_poll_reply_*`: the locked window has disappeared.
_WINDOW_GONE: object = object()
# Time to wait after activating the target app before clicking, so
# the OS has time to make it the key window. Empirically ~80–120 ms
# is enough on a warm app; 200 ms gives generous headroom.
ACTIVATE_SETTLE_S: float = 0.2
# Time after the synthetic click before sending Cmd+V, so the
# contenteditable has time to receive focus from the click event.
CLICK_SETTLE_S: float = 0.1
# Budget for the page to open the WebSocket and land its handshake
# message after we paste the `ws_handshake` op. If this runs out, we
# stay on the QR path for this command and try again next time.
WS_HANDSHAKE_WAIT_S: float = 2.0
# Cross-platform paste keystroke. macOS uses Cmd+V; everywhere else
# uses Ctrl+V. Sent through the SikuliX bridge.
_PASTE_COMBO: str = "cmd+v" if sys.platform == "darwin" else "ctrl+v"

# Process-wide lock around the macOS clipboard + keystroke pipeline.
# Two QR-mode channels in the same process must not race on the
# clipboard / focus-steal sequence; WS-mode channels send through
# their own socket and don't touch this lock.
_CLIPBOARD_LOCK: threading.Lock = threading.Lock()


class CalibrationError(RuntimeError):
    """Raised when no calibration beacon arrives within the timeout."""


class CommandError(RuntimeError):
    """Raised when a command's reply doesn't arrive within the timeout."""


@dataclass(slots=True)
class Channel:
    browsers: frozenset[str] = field(default=DEFAULT_BROWSERS)
    poll_interval: float = DEFAULT_POLL_INTERVAL_S
    default_timeout: float = DEFAULT_TIMEOUT_S
    daemon: Daemon | None = None
    hide_qr: bool = False
    session: str | None = None
    _window_id: int | None = None
    _window_pid: int = 0
    _window_owner: str = ""
    _ws_attempted: bool = False
    _ws_ready: bool = False
    _ws_conn: Any = None
    _ws_attach_event: threading.Event = field(default_factory=threading.Event)
    _ws_pending: dict[str, queue.Queue] = field(default_factory=dict)

    def wait_for_calibration(self, *, timeout: float | None = None) -> str:
        """Poll browser windows for a `[holo:cal:<sid>]` beacon.

        Returns the session id and locks the channel to the window
        that emitted it. Raises `CalibrationError` on timeout.
        """
        budget = timeout if timeout is not None else self.default_timeout
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            for win in self._list_browser_windows():
                marker = title_mod.decode_plain(win.title)
                if marker and marker.startswith("cal:"):
                    self.session = marker[len("cal:") :]
                    self._window_id = win.id
                    self._window_pid = win.pid
                    self._window_owner = win.owner
                    return self.session
            time.sleep(self.poll_interval)
        raise CalibrationError(f"no calibration beacon within {budget}s")

    def send_command(self, cmd: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """Send a JSON command, return the result dict.

        Blocks until a reply with the matching frame id arrives, or
        until `timeout` elapses (default `self.default_timeout`).
        """
        if self.session is None or self._window_id is None:
            raise RuntimeError(
                "Channel has not been calibrated; call wait_for_calibration() first"
            )
        budget = timeout if timeout is not None else self.default_timeout

        if self.daemon is not None and not self._ws_attempted:
            self._bootstrap_ws()

        if self._ws_ready and self._ws_conn is not None:
            return self._send_via_ws(cmd, budget)
        return self._send_via_paste(cmd, budget)

    def _bootstrap_ws(self) -> None:
        """Paste a `ws_handshake` op carrying the daemon's URL+token.

        The page opens a `WebSocket`, sends back a handshake message,
        and the WS server's `_on_ws_attached` flips `_ws_attach_event`.
        We wait briefly here so the very next `_send_via_*` call can
        ride the socket if the round-trip beat the budget.
        """
        self._ws_attempted = True
        if self.daemon is None:
            return
        handshake = {
            "op": "ws_handshake",
            "url": self.daemon.ws_server.popup_url,
            "token": self.daemon.ws_server.token,
        }
        data = json.dumps(handshake).encode("utf-8")
        frame = framing.Frame(session=self.session, type="cmd", data=data)
        self._paste_text(frame.encode())
        if self._ws_attach_event.wait(timeout=WS_HANDSHAKE_WAIT_S):
            self._ws_ready = True

    def _send_via_ws(self, cmd: dict[str, Any], budget: float) -> dict[str, Any]:
        data = json.dumps(cmd).encode("utf-8")
        frame = framing.Frame(session=self.session, type="cmd", data=data)
        msg = json.dumps({"type": "cmd", "frame": frame.encode()})

        reply_q: queue.Queue = queue.Queue(maxsize=1)
        self._ws_pending[frame.id] = reply_q
        try:
            self._ws_conn.send(msg)
            try:
                reply = reply_q.get(timeout=budget)
            except queue.Empty as e:
                raise CommandError(f"no reply for cmd within {budget}s") from e
        finally:
            self._ws_pending.pop(frame.id, None)
        return json.loads(reply.data.decode("utf-8"))

    def _send_via_paste(self, cmd: dict[str, Any], budget: float) -> dict[str, Any]:
        # When stealth mode is on, the bookmarklet must use near-
        # identical colors for the reply QR; piggyback the flag on the
        # command payload. dispatch.js ignores fields it doesn't know,
        # so prefixing with `_` (transport metadata) is safe.
        payload = dict(cmd)
        if self.hide_qr:
            payload["_hide_qr"] = True
        data = json.dumps(payload).encode("utf-8")
        frame = framing.Frame(session=self.session, type="cmd", data=data)
        self._paste_text(frame.encode())
        deadline = time.monotonic() + budget
        # The reply channel is a QR code rendered into the popup's
        # canvas. We capture the window's pixels and run Vision QR
        # detection on each poll. QR-decode round-trip is ~50–100 ms
        # vs ~1 ms for a title read, so we use a slower poll interval.
        qr_poll_interval = max(self.poll_interval, 0.15)
        reply_poller = self._poll_reply_qr if sys.platform == "darwin" else self._poll_reply_title
        while time.monotonic() < deadline:
            reply = reply_poller()
            if reply == _WINDOW_GONE:
                raise CommandError("locked window is no longer present")
            if (
                reply is not None
                and reply.id == frame.id
                and reply.session == self.session
                and reply.type == "result"
            ):
                return json.loads(reply.data.decode("utf-8"))
            time.sleep(qr_poll_interval)
        raise CommandError(f"no reply for cmd within {budget}s")

    def _paste_text(self, text: str) -> None:
        """Activate the popup, copy `text`, simulate Cmd/Ctrl+V into it.

        Serialized process-wide so multiple channels in the same
        daemon don't race on the global clipboard / focus pipeline.

        Two implementations:

        * **Bridge path** (preferred). When a `Daemon` is attached and
          its SikuliX bridge has come up, every step (activate → click
          into popup body → write clipboard → paste keystroke) goes
          through the bridge. Cross-platform; this is the path the
          binary distribution will use.
        * **Legacy path**. On macOS without a bridge, we use the
          osascript / System Events pipeline (`_macos.py`). On other
          platforms without a bridge we just write the clipboard and
          assume the caller keeps the target focused.

        We don't restore the clipboard here: a fast restore can win
        the race against the OS-level paste, and the page's handler
        reads event.clipboardData (a snapshot) anyway.
        """
        bridge = self._bridge()
        if bridge is not None:
            self._paste_text_via_bridge(bridge, text)
            return

        with _CLIPBOARD_LOCK:
            if sys.platform == "darwin" and self._window_owner:
                self._activate_target()
                clipboard.write(text)
                time.sleep(0.05)
                from holo._macos import keystroke_paste

                keystroke_paste(self._window_owner)
            else:
                self._activate_target()
                clipboard.paste(text)

    def _paste_text_via_bridge(self, bridge: Any, text: str) -> None:
        """Drive the activate → click → clipboard → paste sequence through SikuliX."""
        with _CLIPBOARD_LOCK:
            if self._window_owner:
                try:
                    bridge.activate(self._window_owner)
                    time.sleep(ACTIVATE_SETTLE_S)
                except Exception:
                    # If activation fails the click + paste still happen
                    # against the foreground app — that's no worse than
                    # the legacy path's silent degrade.
                    pass

            click_point = self._popup_body_click_point()
            if click_point is not None:
                bridge.click(int(click_point[0]), int(click_point[1]))
                time.sleep(CLICK_SETTLE_S)

            clipboard.write(text)
            time.sleep(0.05)
            bridge.key(_PASTE_COMBO)

    def _bridge(self) -> Any:
        """Return the daemon's SikuliX bridge, or None if unavailable."""
        if self.daemon is None:
            return None
        return self.daemon.bridge

    def _on_ws_attached(self, conn: Any) -> None:
        """Called by the WS server thread once a handshake validates."""
        self._ws_conn = conn
        self._ws_attach_event.set()
        self._ws_ready = True

    def _on_ws_message(self, raw: Any) -> None:
        """Called by the WS server thread for each post-handshake frame."""
        if isinstance(raw, bytes | bytearray):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return
        if not isinstance(raw, str):
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict) or msg.get("type") != "result":
            return
        frame_str = msg.get("frame")
        if not isinstance(frame_str, str):
            return
        try:
            reply = framing.decode(frame_str)
        except framing.FrameError:
            return
        if reply.session != self.session or reply.type != "result":
            return
        pending = self._ws_pending.get(reply.id)
        if pending is not None:
            try:
                pending.put_nowait(reply)
            except queue.Full:
                pass

    def _on_ws_detached(self) -> None:
        """Called by the WS server thread when the socket closes."""
        self._ws_ready = False
        self._ws_conn = None
        self._ws_attach_event.clear()

    def _poll_reply_qr(self) -> framing.Frame | object | None:
        """Capture the locked window and decode any QR code present."""
        from holo._macos import capture_window_qr

        # Confirm the window still exists; capture_window_qr returns
        # None for both 'window gone' and 'no QR yet', so we
        # disambiguate by listing windows separately.
        if self._read_window_title() is None:
            return _WINDOW_GONE
        payload = (
            capture_window_qr(self._window_id, hide_qr=self.hide_qr)
            if self._window_id
            else None
        )
        if not payload:
            return None
        try:
            return framing.decode(payload)
        except framing.FrameError:
            return None

    def _poll_reply_title(self) -> framing.Frame | object | None:
        """Legacy title-channel poll, kept for non-darwin platforms."""
        current = self._read_window_title()
        if current is None:
            return _WINDOW_GONE
        framed_json = title_mod.decode_framed(current)
        if not framed_json:
            return None
        try:
            return framing.decode(framed_json)
        except framing.FrameError:
            return None

    def _list_browser_windows(self) -> list[WindowInfo]:
        return [w for w in list_windows() if w.owner in self.browsers]

    def _read_window_title(self) -> str | None:
        for w in list_windows():
            if w.id == self._window_id:
                return w.title
        return None

    def _activate_target(self) -> None:
        """Bring the locked popup to the foreground and put OS keyboard
        focus inside its contenteditable body before pasting.

        Without activation, the synthesized Cmd+V lands in whatever app
        currently has keyboard focus (the terminal we're running from,
        a different browser window, …). Without the click, Chrome opens
        new popups with OS focus on the address bar — JS `.focus()`
        cannot move OS focus out of browser chrome, so we have to do it
        with a synthetic mouse click. On non-darwin platforms or when
        pid/bounds are unknown this degrades gracefully — callers must
        keep the target window focused themselves.
        """
        if sys.platform != "darwin" or self._window_pid <= 0:
            return
        from holo._macos import activate_pid, click_at

        if not activate_pid(self._window_pid):
            return
        time.sleep(ACTIVATE_SETTLE_S)

        click_point = self._popup_body_click_point()
        if click_point is not None:
            click_at(*click_point)
            time.sleep(CLICK_SETTLE_S)

    def _popup_body_click_point(self) -> tuple[float, float] | None:
        """Pick a point inside the popup body that is safely below the
        URL bar and away from window controls.

        Re-reads the locked window's current bounds — the user may
        have moved or resized the popup since calibration.

        The popup's vertical layout in Chrome:
            0–28 px   titlebar (traffic-light buttons)
            28–78 px  URL bar
            78– px    body / content area

        Targeting the horizontal center at 75 % of window height puts
        the click well inside the body even on a tiny popup, with
        plenty of margin from the URL bar above and the resize handle
        below.
        """
        for w in list_windows():
            if w.id == self._window_id and w.bounds is not None:
                x, y, width, height = w.bounds
                return (x + width / 2.0, y + height * 0.75)
        return None
