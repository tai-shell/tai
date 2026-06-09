"""Tests for holo.channel — orchestration of clipboard + window-title primitives.

The channel itself is platform-neutral (it composes the OS primitives),
so these tests mock both `holo.windows.list_windows` and
`holo.clipboard.paste`. The orchestration tests verify the request/
response loop end-to-end without needing a real browser.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from holo import framing
from holo.channel import CalibrationError, Channel, CommandError
from holo.windows import WindowInfo


def _w(id, title, owner="Google Chrome", pid=0, bounds=None):
    return WindowInfo(id=id, title=title, owner=owner, layer=0, pid=pid, bounds=bounds)


def _make_reply_title(*, frame_id, session, result, original_title="Page"):
    """Build a [holo:1:...] title that the legacy bookmarklet would produce.

    Still used by the non-darwin / title-channel send_command path. On
    darwin the bookmarklet renders replies as QR codes and the daemon
    captures pixels via `_poll_reply_qr`; tests on that path use
    `_make_reply_qr` instead.
    """
    reply_frame = framing.Frame(
        session=session,
        type="result",
        data=json.dumps(result).encode("utf-8"),
        id=frame_id,
    )
    encoded = base64.b64encode(reply_frame.encode().encode("utf-8")).decode("ascii")
    return f"{original_title} [holo:1:{encoded}]"


def _make_reply_qr(*, frame_id, session, result):
    """Encode a reply frame the way the bookmarklet would draw into its QR canvas.

    On darwin, `_poll_reply_qr` calls `capture_window_qr`, which returns
    the QR's payload string — a frame-encoded JSON envelope. Tests that
    patch `capture_window_qr` use this helper to stand in for what the
    bookmarklet would render.
    """
    reply_frame = framing.Frame(
        session=session,
        type="result",
        data=json.dumps(result).encode("utf-8"),
        id=frame_id,
    )
    return reply_frame.encode()


def _capture_returning(payload):
    """side_effect helper that returns payload['value'] regardless of kwargs."""
    return lambda _w, **_kw: payload['value']


@pytest.fixture
def fake_list_windows():
    with patch("holo.channel.list_windows") as m:
        yield m


@pytest.fixture
def fake_paste():
    with patch("holo.clipboard.paste") as m:
        yield m


class TestWaitForCalibration:
    def test_returns_session_from_beacon_and_locks_window(self, fake_list_windows):
        fake_list_windows.return_value = [
            _w(1, "Other - Chrome"),
            _w(42, "GitHub [holo:cal:abc-123]", pid=9876, owner="Google Chrome"),
        ]
        ch = Channel(poll_interval=0.001, default_timeout=0.5)
        assert ch.wait_for_calibration() == "abc-123"
        assert ch._window_id == 42
        assert ch._window_pid == 9876
        assert ch._window_owner == "Google Chrome"

    def test_ignores_non_browser_windows(self, fake_list_windows):
        fake_list_windows.return_value = [
            _w(42, "X [holo:cal:abc]", owner="Some App"),
        ]
        ch = Channel(poll_interval=0.001, default_timeout=0.05)
        with pytest.raises(CalibrationError):
            ch.wait_for_calibration()

    def test_ignores_non_calibration_markers(self, fake_list_windows):
        fake_list_windows.return_value = [
            _w(42, "X [holo:bye:abc]"),
            _w(43, "X [holo:err:something]"),
        ]
        ch = Channel(poll_interval=0.001, default_timeout=0.05)
        with pytest.raises(CalibrationError):
            ch.wait_for_calibration()

    def test_raises_on_timeout_with_no_beacon(self, fake_list_windows):
        fake_list_windows.return_value = [_w(1, "Just a page")]
        ch = Channel(poll_interval=0.001, default_timeout=0.05)
        with pytest.raises(CalibrationError, match="0.05"):
            ch.wait_for_calibration()

    def test_explicit_timeout_overrides_default(self, fake_list_windows):
        fake_list_windows.return_value = [_w(1, "Just a page")]
        ch = Channel(poll_interval=0.001, default_timeout=99)
        with pytest.raises(CalibrationError, match="0.02"):
            ch.wait_for_calibration(timeout=0.02)

    def test_uses_custom_browsers_set(self, fake_list_windows):
        fake_list_windows.return_value = [
            _w(42, "X [holo:cal:custom]", owner="WeirdBrowser"),
        ]
        ch = Channel(
            browsers=frozenset({"WeirdBrowser"}),
            poll_interval=0.001,
            default_timeout=0.5,
        )
        assert ch.wait_for_calibration() == "custom"


class TestSendCommand:
    def test_round_trip_returns_result(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess-1"
        ch._window_id = 42
        # Empty owner forces the cross-platform / pyautogui path
        # exercised by clipboard.paste, which is what these tests mock.
        ch._window_owner = ""

        # Window stays in the list so existence checks pass; the reply
        # arrives via QR (darwin) or title (other platforms).
        current_title = {"value": "Page [holo:cal:sess-1]"}
        fake_list_windows.side_effect = lambda: [_w(42, current_title["value"])]
        qr_payload = {"value": None}

        def respond(text):
            sent = framing.decode(text)
            current_title["value"] = _make_reply_title(
                frame_id=sent.id,
                session="sess-1",
                result={"pong": True},
            )
            qr_payload["value"] = _make_reply_qr(
                frame_id=sent.id,
                session="sess-1",
                result={"pong": True},
            )

        fake_paste.side_effect = respond

        with patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)):
            assert ch.send_command({"op": "ping"}) == {"pong": True}
        fake_paste.assert_called_once()

    def test_ignores_replies_with_mismatched_id(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=0.1)
        ch.session = "sess"
        ch._window_id = 42
        # Pre-populate a stale reply for some other frame id; daemon must ignore.
        stale_title = _make_reply_title(
            frame_id="stale-id", session="sess", result={"pong": True}
        )
        stale_qr = _make_reply_qr(
            frame_id="stale-id", session="sess", result={"pong": True}
        )
        fake_list_windows.return_value = [_w(42, stale_title)]
        with patch("holo._macos.capture_window_qr", return_value=stale_qr):
            with pytest.raises(CommandError):
                ch.send_command({"op": "ping"})

    def test_ignores_replies_with_mismatched_session(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=0.1)
        ch.session = "sess-A"
        ch._window_id = 42
        # A reply from a different session — ignored.
        cur_title = {"value": "Page"}
        fake_list_windows.side_effect = lambda: [_w(42, cur_title["value"])]
        qr_payload = {"value": None}

        def respond(text):
            sent = framing.decode(text)
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id,
                session="sess-B",  # wrong session
                result={"pong": True},
            )
            qr_payload["value"] = _make_reply_qr(
                frame_id=sent.id,
                session="sess-B",
                result={"pong": True},
            )

        fake_paste.side_effect = respond
        with patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)):
            with pytest.raises(CommandError):
                ch.send_command({"op": "ping"})

    def test_raises_if_not_calibrated(self):
        ch = Channel()
        with pytest.raises(RuntimeError, match="calibrated"):
            ch.send_command({"op": "ping"})

    def test_raises_if_window_disappears(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=0.5)
        ch.session = "sess"
        ch._window_id = 42
        # Window list never contains id 42 → daemon should detect and raise.
        fake_list_windows.return_value = [_w(99, "Other")]
        with pytest.raises(CommandError, match="no longer present"):
            ch.send_command({"op": "ping"})

    def test_serializes_command_as_json_in_frame(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        cur_title = {"value": "Page"}
        fake_list_windows.side_effect = lambda: [_w(42, cur_title["value"])]
        captured = {}
        qr_payload = {"value": None}

        def respond(text):
            sent = framing.decode(text)
            captured["frame"] = sent
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id, session="sess", result={"ok": True}
            )
            qr_payload["value"] = _make_reply_qr(
                frame_id=sent.id, session="sess", result={"ok": True}
            )

        fake_paste.side_effect = respond

        with patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)):
            ch.send_command({"op": "read_global", "path": "R2D2_VERSION"})

        sent = captured["frame"]
        assert sent.session == "sess"
        assert sent.type == "cmd"
        assert json.loads(sent.data.decode("utf-8")) == {
            "op": "read_global",
            "path": "R2D2_VERSION",
        }

    def test_hide_qr_flag_propagates_to_payload_and_capture(self, fake_list_windows, fake_paste):
        """When `Channel(hide_qr=True)`, each pasted command frame must
        carry `_hide_qr: true` so the popup paints the reply QR in
        stealth colors, AND the QR poller must pass `hide_qr=True`
        through to `capture_window_qr` so the daemon amplifies the
        captured pixels before running Vision.
        """
        ch = Channel(hide_qr=True, poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        cur_title = {"value": "Page"}
        fake_list_windows.side_effect = lambda: [_w(42, cur_title["value"])]
        captured: dict = {}
        qr_payload = {"value": None}
        capture_kwargs: list[dict] = []

        def respond(text):
            sent = framing.decode(text)
            captured["frame"] = sent
            qr_payload["value"] = _make_reply_qr(
                frame_id=sent.id, session="sess", result={"ok": True}
            )

        def fake_capture(_window_id, **kw):
            capture_kwargs.append(kw)
            return qr_payload["value"]

        fake_paste.side_effect = respond

        with patch("holo._macos.capture_window_qr", side_effect=fake_capture):
            ch.send_command({"op": "ping"})

        # Pasted command body included the stealth flag.
        assert json.loads(captured["frame"].data.decode("utf-8")) == {
            "op": "ping",
            "_hide_qr": True,
        }
        # capture_window_qr was called with hide_qr=True at least once.
        assert any(kw.get("hide_qr") is True for kw in capture_kwargs)


class TestActivation:
    """The daemon must activate the locked window's app before pasting,
    otherwise the synthesized Cmd+V lands in whatever app currently has
    keyboard focus (usually the terminal we're running from).
    """

    def test_activates_then_clicks_then_pastes(self, fake_list_windows, fake_paste):
        import sys

        if sys.platform != "darwin":
            pytest.skip("activation helper is darwin-only")
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        ch._window_pid = 1234
        cur_title = {"value": "Page"}
        # Bounds so _popup_body_click_point computes a click point.
        fake_list_windows.side_effect = lambda: [
            _w(42, cur_title["value"], pid=1234, bounds=(100.0, 50.0, 320.0, 160.0)),
        ]
        order = []
        qr_payload = {"value": None}

        def respond(text):
            order.append(("paste", text[:8]))
            sent = framing.decode(text)
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id, session="sess", result={"pong": True}
            )
            qr_payload["value"] = _make_reply_qr(
                frame_id=sent.id, session="sess", result={"pong": True}
            )

        fake_paste.side_effect = respond

        with (
            patch(
                "holo._macos.activate_pid",
                side_effect=lambda pid: order.append(("activate", pid)) or True,
            ),
            patch(
                "holo._macos.click_at",
                side_effect=lambda x, y: order.append(("click", x, y)),
            ),
            patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)),
            patch("holo.channel.ACTIVATE_SETTLE_S", 0.0),
            patch("holo.channel.CLICK_SETTLE_S", 0.0),
        ):
            ch.send_command({"op": "ping"})

        # Strict ordering: activate, then click into the body, then paste.
        assert order[0] == ("activate", 1234)
        assert order[1][0] == "click"
        # Click should be inside the popup body — center horizontally,
        # 75 % down vertically. bounds = (100, 50, 320, 160) →
        # (100 + 160, 50 + 120) = (260, 170).
        assert order[1] == ("click", 260.0, 170.0)
        assert order[2][0] == "paste"

    def test_skips_activation_when_pid_unknown(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        ch._window_pid = 0  # unknown (e.g. older calibration path)
        cur_title = {"value": "Page"}
        fake_list_windows.side_effect = lambda: [_w(42, cur_title["value"])]
        called = []
        qr_payload = {"value": None}

        def respond(text):
            sent = framing.decode(text)
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id, session="sess", result={"pong": True}
            )
            qr_payload["value"] = _make_reply_qr(
                frame_id=sent.id, session="sess", result={"pong": True}
            )

        fake_paste.side_effect = respond

        with (
            patch("holo._macos.activate_pid", side_effect=lambda p: called.append(p)),
            patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)),
        ):
            ch.send_command({"op": "ping"})

        assert called == []

    def test_skips_click_when_bounds_unknown(self, fake_list_windows, fake_paste):
        import sys

        if sys.platform != "darwin":
            pytest.skip("activation helper is darwin-only")
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        ch._window_pid = 1234
        ch._window_owner = ""  # force the pyautogui path
        cur_title = {"value": "Page"}
        # No bounds — _popup_body_click_point should return None.
        fake_list_windows.side_effect = lambda: [
            _w(42, cur_title["value"], pid=1234, bounds=None),
        ]
        clicks = []
        qr_payload = {"value": None}

        def respond(text):
            sent = framing.decode(text)
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id, session="sess", result={"pong": True}
            )
            qr_payload["value"] = _make_reply_qr(
                frame_id=sent.id, session="sess", result={"pong": True}
            )

        fake_paste.side_effect = respond

        with (
            patch("holo._macos.activate_pid", return_value=True),
            patch("holo._macos.click_at", side_effect=lambda x, y: clicks.append((x, y))),
            patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)),
            patch("holo.channel.ACTIVATE_SETTLE_S", 0.0),
            patch("holo.channel.CLICK_SETTLE_S", 0.0),
        ):
            ch.send_command({"op": "ping"})

        # Activate fired, but no click because bounds were unavailable.
        assert clicks == []

    def test_darwin_path_uses_osascript_keystroke(self, fake_list_windows):
        """On macOS, send_command should use the keystroke_paste path
        (osascript + System Events) rather than pyautogui-via-clipboard.paste.
        """
        import sys

        if sys.platform != "darwin":
            pytest.skip("osascript keystroke path is darwin-only")

        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        ch._window_pid = 1234
        ch._window_owner = "Google Chrome"
        cur_title = {"value": "Page"}
        fake_list_windows.side_effect = lambda: [
            _w(42, cur_title["value"], pid=1234, owner="Google Chrome",
               bounds=(100.0, 50.0, 320.0, 160.0)),
        ]
        order = []

        captured = {}
        qr_payload = {"value": None}

        def fake_write(text):
            order.append(("clipboard_write", text[:8]))
            captured["frame"] = framing.decode(text)

        def fake_keystroke(name):
            order.append(("keystroke", name))
            # Simulate the popup receiving the paste and rendering the
            # reply as a QR code, using the same frame id we just sent.
            cur_title["value"] = _make_reply_title(
                frame_id=captured["frame"].id,
                session="sess",
                result={"ok": True},
            )
            qr_payload["value"] = _make_reply_qr(
                frame_id=captured["frame"].id,
                session="sess",
                result={"ok": True},
            )
            return True

        with (
            patch(
                "holo._macos.activate_pid",
                side_effect=lambda pid: order.append(("activate", pid)) or True,
            ),
            patch("holo._macos.click_at", side_effect=lambda x, y: order.append(("click", x, y))),
            patch("holo._macos.keystroke_paste", side_effect=fake_keystroke),
            patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)),
            patch("holo.clipboard.write", side_effect=fake_write),
            patch("holo.channel.ACTIVATE_SETTLE_S", 0.0),
            patch("holo.channel.CLICK_SETTLE_S", 0.0),
        ):
            result = ch.send_command({"op": "ping"})

        assert result == {"ok": True}
        # Strict ordering: activate → click → clipboard write → osascript keystroke
        kinds = [t[0] for t in order]
        assert kinds == ["activate", "click", "clipboard_write", "keystroke"]
        assert order[3] == ("keystroke", "Google Chrome")


class TestWsPath:
    """Once a Channel has a Daemon attached and a WebSocket has handshook
    in, send_command should ride the socket — no clipboard, no QR poll.
    The first send_command pastes a `ws_handshake` op via clipboard;
    subsequent commands skip the paste entirely.
    """

    def test_first_send_pastes_handshake_then_uses_ws(self, fake_list_windows):
        import json as _json
        import queue as _queue
        import threading

        from websockets.sync.client import connect

        from holo.daemon import Daemon

        d = Daemon()
        try:
            ch = Channel(daemon=d, poll_interval=0.001, default_timeout=2.0)
            ch.session = "sid-ws"
            ch._window_id = 99
            ch._window_owner = ""  # cross-platform paste path
            d.registry.register("sid-ws", ch)

            fake_list_windows.return_value = [_w(99, "Page")]

            # Stand in for the bookmarklet: when the daemon pastes the
            # ws_handshake frame, decode it, open a WS, send the handshake
            # message, then echo subsequent cmd frames as result frames.
            sent_via_paste: list[str] = []
            page_thread_done = threading.Event()
            page_results: dict[str, dict] = {}

            def fake_paste(text):
                sent_via_paste.append(text)
                frame = framing.decode(text)
                cmd = _json.loads(frame.data.decode("utf-8"))
                assert cmd["op"] == "ws_handshake"
                threading.Thread(
                    target=_run_fake_page,
                    args=(cmd["url"], cmd["token"], "sid-ws", page_results, page_thread_done),
                    daemon=True,
                ).start()

            with patch("holo.clipboard.paste", side_effect=fake_paste):
                # First send_command bootstraps WS via paste, then sends "ping" via WS.
                result = ch.send_command({"op": "ping"})

            assert result == {"pong": True}
            assert len(sent_via_paste) == 1, "only the handshake should hit clipboard"
            assert ch._ws_ready

            # Second send_command must NOT paste again — it goes straight over WS.
            with patch("holo.clipboard.paste", side_effect=AssertionError("no paste")):
                result2 = ch.send_command({"op": "ping"})
            assert result2 == {"pong": True}

            # Let the fake page thread finish cleanly.
            page_thread_done.wait(timeout=1.0)
            _ = _queue, connect  # keep imports referenced
        finally:
            d.shutdown()

    def test_ws_handshake_timeout_falls_back_to_qr(self, fake_list_windows, fake_paste):
        """If no WS handshake lands within WS_HANDSHAKE_WAIT_S, the
        channel must stay on the QR/title path instead of hanging.
        """
        from holo.daemon import Daemon

        d = Daemon()
        try:
            ch = Channel(daemon=d, poll_interval=0.001, default_timeout=2.0)
            ch.session = "sid-fallback"
            ch._window_id = 7
            ch._window_owner = ""
            d.registry.register("sid-fallback", ch)

            cur_title = {"value": "Page"}
            fake_list_windows.side_effect = lambda: [_w(7, cur_title["value"])]
            qr_payload = {"value": None}

            def respond(text):
                # Decode the *second* paste (the actual cmd) — the first
                # paste is the ws_handshake which we deliberately ignore
                # to exercise the fallback path.
                sent = framing.decode(text)
                cmd = json.loads(sent.data.decode("utf-8"))
                if cmd.get("op") == "ws_handshake":
                    return  # never connect — force timeout
                cur_title["value"] = _make_reply_title(
                    frame_id=sent.id, session="sid-fallback", result={"pong": True}
                )
                qr_payload["value"] = _make_reply_qr(
                    frame_id=sent.id, session="sid-fallback", result={"pong": True}
                )

            fake_paste.side_effect = respond

            with (
                patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)),
                patch("holo.channel.WS_HANDSHAKE_WAIT_S", 0.1),
            ):
                result = ch.send_command({"op": "ping"})

            assert result == {"pong": True}
            assert not ch._ws_ready
        finally:
            d.shutdown()


def _run_fake_page(agent_url, token, sid, results, done_event):
    """Stand-in for the in-page bookmarklet on the WS path.

    `agent_url` is the http://… URL the daemon sends in the
    `ws_handshake` op. The real bookmarklet would load it as an
    iframe and the iframe would WebSocket back to its own origin;
    this test shortcuts that by deriving the ws:// URL directly
    and connecting from the test process.
    """
    import json as _json
    from urllib.parse import urlparse

    from websockets.sync.client import connect

    parsed = urlparse(agent_url)
    ws_url = f"ws://{parsed.netloc}/"
    with connect(ws_url) as ws:
        ws.send(_json.dumps({"type": "handshake", "sid": sid, "token": token}))
        ack = _json.loads(ws.recv(timeout=2.0))
        assert ack == {"type": "handshake_ack"}
        # Answer cmd frames until the test closes the socket on us.
        try:
            for raw in ws:
                msg = _json.loads(raw)
                if msg.get("type") != "cmd":
                    continue
                inbound = framing.decode(msg["frame"])
                reply = framing.Frame(
                    session=inbound.session,
                    type="result",
                    data=_json.dumps({"pong": True}).encode("utf-8"),
                    id=inbound.id,
                )
                ws.send(_json.dumps({"type": "result", "frame": reply.encode()}))
                results[inbound.id] = {"pong": True}
        finally:
            done_event.set()


class _StubBridge:
    """Minimal stand-in for `holo.bridge.BridgeClient` for tests."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def activate(self, name: str):
        self.calls.append(("activate", name))
        return {"focused": True, "name": name}

    def click(self, x: int, y: int, *, modifiers=None):
        self.calls.append(("click", x, y))
        return {"clicked": True, "x": x, "y": y}

    def key(self, combo: str):
        self.calls.append(("key", combo))
        return {"sent": combo}

    def type_text(self, text: str):
        self.calls.append(("type_text", text))
        return {"typed_chars": len(text)}


class TestBridgePath:
    """When a Daemon has `enable_screen=True` and its bridge has come up,
    the paste pipeline must route every step (activate, click, paste
    keystroke) through the SikuliX bridge — not through `_macos.py`."""

    def test_paste_routes_activate_click_key_through_bridge(self, fake_list_windows):
        from holo.daemon import Daemon

        bridge = _StubBridge()
        d = Daemon(enable_screen=True)
        # Inject the stub bridge by short-circuiting the lazy-start.
        d._bridge = bridge  # type: ignore[assignment]
        d._bridge_attempted = True

        try:
            ch = Channel(daemon=d, poll_interval=0.001, default_timeout=2.0)
            ch.session = "sid-bridge"
            ch._window_id = 7
            ch._window_pid = 9999
            ch._window_owner = "Google Chrome"
            # Skip the WS handshake bootstrap — that path is exercised in
            # TestWsPath and would otherwise double the paste sequence.
            ch._ws_attempted = True

            cur_title = {"value": "Page"}
            qr_payload = {"value": None}
            fake_list_windows.side_effect = lambda: [
                _w(
                    7,
                    cur_title["value"],
                    pid=9999,
                    owner="Google Chrome",
                    bounds=(100.0, 50.0, 320.0, 160.0),
                ),
            ]
            captured: dict = {}

            def fake_clipboard_write(text):
                bridge.calls.append(("clipboard_write", text[:8]))
                captured["frame"] = framing.decode(text)
                cur_title["value"] = _make_reply_title(
                    frame_id=captured["frame"].id,
                    session="sid-bridge",
                    result={"pong": True},
                )
                qr_payload["value"] = _make_reply_qr(
                    frame_id=captured["frame"].id,
                    session="sid-bridge",
                    result={"pong": True},
                )

            with (
                patch("holo.clipboard.write", side_effect=fake_clipboard_write),
                patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)),
                patch("holo.channel.ACTIVATE_SETTLE_S", 0.0),
                patch("holo.channel.CLICK_SETTLE_S", 0.0),
                patch("holo.channel.WS_HANDSHAKE_WAIT_S", 0.05),
            ):
                result = ch.send_command({"op": "ping"})

            assert result == {"pong": True}
            kinds = [c[0] for c in bridge.calls]
            # Strict ordering: activate → click → clipboard write → key
            assert kinds == ["activate", "click", "clipboard_write", "key"]
            assert bridge.calls[0] == ("activate", "Google Chrome")
            # The click point is centred horizontally, 75% of the way down.
            assert bridge.calls[1] == ("click", 260, 170)
            # Paste combo is platform-specific — match either Cmd+V or Ctrl+V.
            assert bridge.calls[3][0] == "key"
            assert bridge.calls[3][1] in {"cmd+v", "ctrl+v"}
        finally:
            d._bridge = None  # don't let shutdown call into the stub
            d.shutdown()

    def test_enable_screen_false_keeps_legacy_macos_path(self, fake_list_windows):
        """When `enable_screen=False`, channels must NEVER touch the bridge.

        Existing macOS users without OpenJDK installed depend on this —
        the legacy `_macos.py` path stays default until binary builds
        bundle the JVM as a bundled dep.
        """
        import sys as _sys

        from holo.daemon import Daemon

        if _sys.platform != "darwin":
            pytest.skip("legacy path is darwin-only")

        d = Daemon(enable_screen=False)
        try:
            assert d.bridge is None  # never starts when opted out
            ch = Channel(daemon=d, poll_interval=0.001, default_timeout=2.0)
            ch.session = "sid-legacy"
            ch._window_id = 8
            ch._window_pid = 1234
            ch._window_owner = "Google Chrome"
            ch._ws_attempted = True  # skip handshake bootstrap (see TestWsPath)
            cur_title = {"value": "Page"}
            qr_payload = {"value": None}
            fake_list_windows.side_effect = lambda: [
                _w(8, cur_title["value"], pid=1234, owner="Google Chrome",
                   bounds=(0.0, 0.0, 320.0, 160.0)),
            ]
            captured: dict = {}
            order: list = []

            def fake_keystroke(name):
                order.append(("keystroke", name))
                cur_title["value"] = _make_reply_title(
                    frame_id=captured["frame"].id,
                    session="sid-legacy",
                    result={"ok": True},
                )
                qr_payload["value"] = _make_reply_qr(
                    frame_id=captured["frame"].id,
                    session="sid-legacy",
                    result={"ok": True},
                )

            def fake_clipboard_write(text):
                captured["frame"] = framing.decode(text)

            def fake_activate(p):
                order.append(("activate", p))
                return True

            def fake_click(x, y):
                order.append(("click", x, y))

            with (
                patch("holo._macos.activate_pid", side_effect=fake_activate),
                patch("holo._macos.click_at", side_effect=fake_click),
                patch("holo._macos.keystroke_paste", side_effect=fake_keystroke),
                patch("holo._macos.capture_window_qr", side_effect=_capture_returning(qr_payload)),
                patch("holo.clipboard.write", side_effect=fake_clipboard_write),
                patch("holo.channel.ACTIVATE_SETTLE_S", 0.0),
                patch("holo.channel.CLICK_SETTLE_S", 0.0),
                patch("holo.channel.WS_HANDSHAKE_WAIT_S", 0.05),
            ):
                result = ch.send_command({"op": "ping"})

            assert result == {"ok": True}
            kinds = [t[0] for t in order]
            assert kinds == ["activate", "click", "keystroke"]
        finally:
            d.shutdown()

    def test_enable_screen_true_but_unavailable_falls_back(self):
        """If `enable_screen=True` but the bridge fails to start (no jar / no
        JDK), `daemon.bridge` returns None and channels must fall through
        cleanly to the legacy path."""
        from unittest.mock import patch

        from holo.bridge import BridgeMissingError
        from holo.daemon import Daemon

        d = Daemon(enable_screen=True)
        try:
            with patch("holo.bridge.BridgeClient.start", side_effect=BridgeMissingError("no jar")):
                assert d.bridge is None
                # Second call should not retry — `_bridge_attempted` is sticky.
                assert d.bridge is None
        finally:
            d.shutdown()
