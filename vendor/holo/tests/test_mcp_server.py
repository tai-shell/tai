"""Unit tests for holo.mcp_server.

The tool handlers are exercised against a fake `Daemon` that owns a
real `ChannelRegistry` plus stub Channel objects — no WS server, no
clipboard, no browser. Goal is to lock the MCP-side surface (arg
shapes, error mapping, transport reporting) while reusing the real
Channel/Daemon paths in their own test files.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from holo.channel import CalibrationError, CommandError
from holo.mcp_server import HoloMCPServer, build_server
from holo.registry import ChannelRegistry


@pytest.fixture(autouse=True)
def _stub_discover_handle():
    """Replace the persistent zeroconf browser that `HoloMCPServer`
    eagerly starts in __init__ with a no-op stub.

    Without this, every test that constructs `HoloMCPServer()` would
    open a real multicast socket and register a real `ServiceBrowser`
    — bleed-through of zeroconf callback threads into other tests was
    surfacing as `NotRunningException` in unrelated cases.

    Tests that exercise discover behaviour shadow this with their own
    `with patch("holo.discover._start_browser", ...)` block; the inner
    patch takes precedence for its scope, then the autouse stub takes
    over again.
    """
    from holo.discover import SessionStore

    class _FakeZC:
        def close(self) -> None:
            pass

    with (
        patch(
            "holo.discover._start_browser",
            return_value=(_FakeZC(), None, SessionStore()),
        ),
        patch(
            "holo.discover._start_stale_sweeper",
            return_value=MagicMock(),
        ),
    ):
        yield


class _StubChannel:
    """Stand-in for `holo.channel.Channel` with just the bits MCP reads."""

    def __init__(
        self,
        sid: str,
        *,
        window_id: int = 42,
        window_owner: str = "Google Chrome",
        ws_ready: bool = False,
        replies: list[Any] | None = None,
    ) -> None:
        self.session = sid
        self._window_id = window_id
        self._window_owner = window_owner
        self._ws_ready = ws_ready
        # Either a list of pre-baked replies or exceptions (popped FIFO),
        # or None to default to {"pong": True} for every call.
        self._replies = replies
        self.calls: list[tuple[dict[str, Any], float | None]] = []

    def send_command(
        self, cmd: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((cmd, timeout))
        if self._replies is None:
            return {"pong": True}
        item = self._replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _StubBridge:
    """Stand-in for the SikuliX bridge — records calls, returns canned data."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_screenshot: bytes = b"\x89PNG-stub"
        self.next_find: dict | None = {
            "x": 50,
            "y": 60,
            "width": 30,
            "height": 30,
            "score": 0.95,
        }

    def activate(self, name):
        self.calls.append(("activate", {"name": name}))
        return {"focused": True, "name": name}

    def click(self, x, y, *, modifiers=None):
        self.calls.append(("click", {"x": x, "y": y, "modifiers": modifiers or []}))
        return {"clicked": True, "x": x, "y": y}

    def key(self, combo):
        self.calls.append(("key", {"combo": combo}))
        return {"sent": combo}

    def type_text(self, text):
        self.calls.append(("type_text", {"text": text}))
        return {"typed_chars": len(text)}

    def scroll(self, x, y, *, direction="down", steps=3):
        self.calls.append(
            ("scroll", {"x": x, "y": y, "direction": direction, "steps": steps})
        )
        return {
            "scrolled": True,
            "x": x,
            "y": y,
            "direction": direction,
            "steps": steps,
        }

    def mouse_move(self, x, y):
        self.calls.append(("mouse_move", {"x": x, "y": y}))
        return {"moved": True, "x": x, "y": y}

    def screenshot(self, *, region=None, timeout=15.0):
        self.calls.append(("screenshot", {"region": region}))
        return self.next_screenshot

    def find_image(self, needle, *, region=None, score=0.7, timeout=15.0):
        self.calls.append(("find_image", {"needle": needle, "region": region, "score": score}))
        return self.next_find

    def find_image_path(self, path, *, region=None, score=0.7, timeout=15.0):
        self.calls.append(
            ("find_image_path", {"path": str(path), "region": region, "score": score})
        )
        return self.next_find

    def user_capture(self, *, prompt="", timeout=60.0):
        self.calls.append(("user_capture", {"prompt": prompt, "timeout": timeout}))
        # Default: return cancelled. Tests override `next_capture` for success.
        return getattr(self, "next_capture", {"cancelled": True, "reason": "stub"})


class _FakeDaemon:
    def __init__(self, *, bridge: _StubBridge | None = None) -> None:
        self.registry = ChannelRegistry()
        self.shutdown_called = False
        self.next_calibrations: list[Any] = []
        self.bridge = bridge

    def calibrate(self, *, timeout: float | None = None) -> _StubChannel:
        if not self.next_calibrations:
            raise AssertionError("test did not queue a calibration result")
        item = self.next_calibrations.pop(0)
        if isinstance(item, BaseException):
            raise item
        self.registry.register(item.session, item)
        return item

    def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture
def server_with_fake() -> tuple[HoloMCPServer, _FakeDaemon]:
    server = HoloMCPServer()
    fake = _FakeDaemon()
    server._daemon = fake  # bypass lazy init — never construct a real Daemon in tests
    return server, fake


class TestCalibrate:
    def test_returns_channel_descriptor(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A", window_id=7))

        result = server.calibrate(timeout=1.0)

        assert result == {
            "sid": "sid-A",
            "window_id": 7,
            "window_owner": "Google Chrome",
            "transport": "qr",
        }

    def test_timeout_becomes_runtime_error(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(CalibrationError("no beacon within 1s"))

        with pytest.raises(RuntimeError, match="calibration timeout"):
            server.calibrate(timeout=1.0)

    def test_fast_path_returns_existing_channel_without_blocking(
        self, server_with_fake
    ):
        """When the registry is non-empty, calibrate returns the most
        recent channel immediately. Cross-host setups depend on this:
        the human calibrates locally on the daemon's machine, then a
        remote agent that connects in shouldn't have to re-trigger the
        bookmarklet — list/use the existing channel.
        """
        server, fake = server_with_fake
        existing = _StubChannel("sid-existing", window_id=99, ws_ready=True)
        fake.registry.register(existing.session, existing)

        # No queued calibrations: if the fast path were missing, the
        # fake's `calibrate()` would assert.
        result = server.calibrate(timeout=1.0)

        assert result == {
            "sid": "sid-existing",
            "window_id": 99,
            "window_owner": "Google Chrome",
            "transport": "ws",
        }

    def test_fast_path_picks_most_recent_channel(self, server_with_fake):
        server, fake = server_with_fake
        fake.registry.register("sid-old", _StubChannel("sid-old"))
        fake.registry.register(
            "sid-new", _StubChannel("sid-new", window_id=42, ws_ready=True)
        )

        result = server.calibrate()

        assert result["sid"] == "sid-new"


class TestListAndDrop:
    def test_list_channels_snapshots_registry(self, server_with_fake):
        server, fake = server_with_fake
        fake.registry.register("sid-A", _StubChannel("sid-A", window_id=1))
        fake.registry.register(
            "sid-B", _StubChannel("sid-B", window_id=2, ws_ready=True)
        )

        listed = server.list_channels()

        sids = sorted(ch["sid"] for ch in listed["channels"])
        assert sids == ["sid-A", "sid-B"]
        by_sid = {ch["sid"]: ch for ch in listed["channels"]}
        assert by_sid["sid-A"]["transport"] == "qr"
        assert by_sid["sid-B"]["transport"] == "ws"

    def test_drop_channel_removes_from_registry(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A"))
        server.calibrate()

        result = server.drop_channel("sid-A")

        assert result == {"ok": True, "sid": "sid-A"}
        assert fake.registry.lookup("sid-A") is None

    def test_drop_unknown_sid_raises(self, server_with_fake):
        server, _ = server_with_fake
        with pytest.raises(ValueError, match="no channel for sid"):
            server.drop_channel("nope")


class TestSendCommand:
    def test_round_trip_returns_result_and_transport(self, server_with_fake):
        server, fake = server_with_fake
        ch = _StubChannel("sid-A", ws_ready=True, replies=[{"value": "Hello"}])
        fake.next_calibrations.append(ch)
        server.calibrate()

        out = server.send_command(
            "sid-A", {"op": "read_global", "path": "document.title"}, timeout=2.0
        )

        assert out == {
            "sid": "sid-A",
            "transport": "ws",
            "result": {"value": "Hello"},
        }
        assert ch.calls == [
            ({"op": "read_global", "path": "document.title"}, 2.0)
        ]

    def test_unknown_sid_raises_value_error(self, server_with_fake):
        server, _ = server_with_fake
        with pytest.raises(ValueError, match="no channel for sid"):
            server.send_command("missing", {"op": "ping"})

    def test_command_must_be_dict(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A"))
        server.calibrate()
        with pytest.raises(ValueError, match="must be an object"):
            server.send_command("sid-A", "ping")  # type: ignore[arg-type]

    def test_command_requires_op_string(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A"))
        server.calibrate()
        with pytest.raises(ValueError, match="op"):
            server.send_command("sid-A", {"path": "x"})
        with pytest.raises(ValueError, match="op"):
            server.send_command("sid-A", {"op": ""})

    def test_command_error_becomes_runtime_error(self, server_with_fake):
        server, fake = server_with_fake
        ch = _StubChannel(
            "sid-A", replies=[CommandError("no reply for cmd within 1s")]
        )
        fake.next_calibrations.append(ch)
        server.calibrate()

        with pytest.raises(RuntimeError, match="command failed"):
            server.send_command("sid-A", {"op": "ping"}, timeout=1.0)


class TestPingAndReadGlobal:
    def test_ping_delegates_to_send_command(self, server_with_fake):
        server, fake = server_with_fake
        ch = _StubChannel("sid-A", replies=[{"pong": True}])
        fake.next_calibrations.append(ch)
        server.calibrate()

        out = server.ping("sid-A", timeout=3.0)

        assert out["result"] == {"pong": True}
        assert ch.calls == [({"op": "ping"}, 3.0)]

    def test_read_global_passes_path(self, server_with_fake):
        server, fake = server_with_fake
        ch = _StubChannel("sid-A", replies=[{"value": 7}])
        fake.next_calibrations.append(ch)
        server.calibrate()

        out = server.read_global("sid-A", "R2D2_VERSION", timeout=4.0)

        assert out["result"] == {"value": 7}
        assert ch.calls == [
            ({"op": "read_global", "path": "R2D2_VERSION"}, 4.0)
        ]

    def test_read_global_rejects_empty_path(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A"))
        server.calibrate()
        with pytest.raises(ValueError, match="path"):
            server.read_global("sid-A", "")


class TestShutdownAndBuild:
    def test_shutdown_propagates_to_daemon_and_clears(self, server_with_fake):
        server, fake = server_with_fake
        # Touch the daemon so it is materialised, then shut down.
        _ = server.daemon
        server.shutdown()
        assert fake.shutdown_called is True
        assert server._daemon is None

    def test_shutdown_no_op_when_daemon_never_created(self):
        server = HoloMCPServer()
        server.shutdown()  # must not raise

    def test_build_server_registers_expected_tools(self):
        mcp, holo = build_server()
        try:
            # FastMCP exposes registered tools via the async list_tools() API.
            import asyncio

            tools = asyncio.run(mcp.list_tools())
            names = {t.name for t in tools}
            assert names == {
                "calibrate",
                "list_channels",
                "drop_channel",
                "ping",
                "read_global",
                "send_command",
                "app_activate",
                "screen_click",
                "screen_type",
                "screen_key",
                "screen_scroll",
                "screen_move",
                "screen_shot",
                "screen_find_image",
                "screen_user_capture",
                "browser_navigate",
                "browser_new_tab",
                "browser_close_active_tab",
                "browser_activate_tab",
                "browser_list_tabs",
                "browser_read_active_url",
                "browser_read_active_title",
                "browser_reload",
                "browser_back",
                "browser_forward",
                "browser_execute_js",
                "bookmarklet_query",
                "ui_template_capture",
                "ui_template_list",
                "ui_template_find",
                "ui_template_click",
                "ui_template_delete",
                "holo_discover_sessions",
                "holo_fetch_capabilities",
                "holo_tunnel_up",
                "holo_tunnel_down",
                "holo_launch_ide",
            }
        finally:
            holo.shutdown()

    def test_no_bookmarklet_omits_channel_tools(self):
        """`--no-bookmarklet` mode drops the seven channel-dependent tools
        (calibrate / list_channels / drop_channel / ping / read_global /
        send_command / bookmarklet_query) but keeps screen, template, and
        AppleScript browser ops. Used by agents that never touch the
        bookmarklet — Slack / desktop orchestrators."""
        import asyncio

        mcp, holo = build_server(no_bookmarklet=True)
        try:
            tools = asyncio.run(mcp.list_tools())
            names = {t.name for t in tools}
            channel_tools = {
                "calibrate",
                "list_channels",
                "drop_channel",
                "ping",
                "read_global",
                "send_command",
                "bookmarklet_query",
            }
            assert names.isdisjoint(channel_tools), (
                f"channel tools should be omitted: {names & channel_tools}"
            )
            # Sanity: surfaces that don't depend on the channel still load.
            assert {
                "app_activate",
                "screen_click",
                "screen_scroll",
                "screen_move",
                "screen_shot",
                "browser_navigate",
                "browser_execute_js",
                "ui_template_capture",
                "ui_template_click",
            }.issubset(names)
        finally:
            holo.shutdown()

    def test_no_bookmarklet_skips_ws_server(self):
        """The Daemon should never spin up its WS server when the flag is
        on — the flag's whole point is to avoid binding a port that
        nothing will use."""
        from holo.daemon import Daemon

        d = Daemon(no_bookmarklet=True)
        try:
            assert d.ws_server is None
        finally:
            d.shutdown()  # must not raise even with ws_server=None

    def test_no_bookmarklet_calibrate_raises(self):
        """`Daemon.calibrate()` must raise rather than silently hanging
        in no-bookmarklet mode — there's no WS server to receive the
        beacon and no popup serving infrastructure."""
        from holo.daemon import Daemon

        d = Daemon(no_bookmarklet=True)
        try:
            with pytest.raises(RuntimeError, match="no-bookmarklet"):
                d.calibrate(timeout=0.1)
        finally:
            d.shutdown()

    def test_no_browser_omits_browser_tools(self):
        """`--no-browser` drops every browser_* AppleScript tool.
        Suits the input-only peer in the split-input-proxy topology
        (the machine that's just driving SikuliX on behalf of a
        corporate-locked peer) — it never needs Chrome control."""
        import asyncio

        mcp, holo = build_server(no_browser=True)
        try:
            tools = asyncio.run(mcp.list_tools())
            names = {t.name for t in tools}
            browser_tools = {
                "browser_navigate",
                "browser_new_tab",
                "browser_close_active_tab",
                "browser_activate_tab",
                "browser_list_tabs",
                "browser_read_active_url",
                "browser_read_active_title",
                "browser_reload",
                "browser_back",
                "browser_forward",
                "browser_execute_js",
            }
            assert names.isdisjoint(browser_tools), (
                f"browser tools should be omitted: {names & browser_tools}"
            )
            # Sanity: input-side tools still load.
            assert {
                "screen_click",
                "screen_key",
                "screen_type",
                "screen_scroll",
                "screen_move",
                "app_activate",
            }.issubset(names)
        finally:
            holo.shutdown()


class TestHoloLaunchIDE:
    """`holo_launch_ide` spawns the SikuliX IDE as a CHILD of the MCP
    server so macOS Accessibility / Screen-Recording grants on the
    `holo` binary cover the IDE. Tests mock subprocess.Popen + the jar
    resolver so no JVM actually starts."""

    def _patch_launch_path(self, monkeypatch, *, java="/opt/homebrew/bin/java", jar_path=None):
        import shutil

        from holo import bridge

        if jar_path is None:
            jar_path = "/cache/sikulixide-2.0.5.jar"
        monkeypatch.setattr(shutil, "which", lambda binary: java if binary == "java" else None)
        monkeypatch.setattr(bridge, "ensure_jar", lambda **kw: jar_path)

    def test_happy_path_returns_pid_jar_java(self, monkeypatch):
        self._patch_launch_path(monkeypatch)

        captured = {}

        class FakeProc:
            pid = 12345

            def __init__(self, argv, *a, **kw):
                captured["argv"] = list(argv)
                captured["kwargs"] = kw

            def poll(self):
                return None

        import subprocess as _sub
        monkeypatch.setattr(_sub, "Popen", FakeProc)

        server = HoloMCPServer()
        try:
            out = server.holo_launch_ide()
            assert out == {
                "pid": 12345,
                "jar": "/cache/sikulixide-2.0.5.jar",
                "java": "/opt/homebrew/bin/java",
                "already_running": False,
            }
            # CRITICAL: no start_new_session=True — that would force
            # macOS to re-evaluate TCC against `java` and break the
            # parent-process inheritance the user expects.
            assert "start_new_session" not in captured["kwargs"]
            assert captured["argv"] == [
                "/opt/homebrew/bin/java", "-jar", "/cache/sikulixide-2.0.5.jar",
            ]
        finally:
            server.shutdown()

    def test_second_call_returns_already_running(self, monkeypatch):
        self._patch_launch_path(monkeypatch)

        call_count = {"n": 0}

        class FakeProc:
            pid = 99

            def __init__(self, *a, **kw):
                call_count["n"] += 1

            def poll(self):
                return None  # still alive

        import subprocess as _sub
        monkeypatch.setattr(_sub, "Popen", FakeProc)

        server = HoloMCPServer()
        try:
            first = server.holo_launch_ide()
            second = server.holo_launch_ide()
            assert first["already_running"] is False
            assert second["already_running"] is True
            assert second["pid"] == first["pid"]
            assert call_count["n"] == 1  # only spawned once
        finally:
            server.shutdown()

    def test_relaunch_after_ide_died(self, monkeypatch):
        """If the user closed the IDE between calls, the next call
        should spawn a fresh JVM instead of returning a stale PID."""
        self._patch_launch_path(monkeypatch)

        alive = {"value": True}
        spawn_count = {"n": 0}

        class FakeProc:
            def __init__(self, *a, **kw):
                spawn_count["n"] += 1
                self.pid = 1000 + spawn_count["n"]

            def poll(self):
                return None if alive["value"] else 0

        import subprocess as _sub
        monkeypatch.setattr(_sub, "Popen", FakeProc)

        server = HoloMCPServer()
        try:
            first = server.holo_launch_ide()
            alive["value"] = False  # simulate IDE quit
            second = server.holo_launch_ide()
            assert first["pid"] == 1001
            assert second["pid"] == 1002
            assert second["already_running"] is False
            assert spawn_count["n"] == 2
        finally:
            server.shutdown()

    def test_raises_when_java_missing(self, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _: None)

        server = HoloMCPServer()
        try:
            with pytest.raises(RuntimeError, match="`java` not on PATH"):
                server.holo_launch_ide()
        finally:
            server.shutdown()

    def test_raises_on_bridge_error(self, monkeypatch):
        import shutil

        from holo import bridge
        monkeypatch.setattr(shutil, "which", lambda _: "/opt/homebrew/bin/java")

        def boom(**kw):
            raise bridge.BridgeMissingError("jar fetch blocked")

        monkeypatch.setattr(bridge, "ensure_jar", boom)

        server = HoloMCPServer()
        try:
            with pytest.raises(RuntimeError, match="jar fetch blocked"):
                server.holo_launch_ide()
        finally:
            server.shutdown()

    def test_shutdown_terminates_running_ide(self, monkeypatch):
        self._patch_launch_path(monkeypatch)

        events = []

        class FakeProc:
            pid = 7777
            _alive = True

            def __init__(self, *a, **kw):
                pass

            def poll(self):
                return None if self._alive else 0

            def terminate(self):
                events.append("terminate")
                self._alive = False

            def wait(self, timeout=None):
                events.append(("wait", timeout))
                return 0

            def kill(self):
                events.append("kill")

        import subprocess as _sub
        monkeypatch.setattr(_sub, "Popen", FakeProc)

        server = HoloMCPServer()
        server.holo_launch_ide()
        server.shutdown()
        # On clean shutdown we send terminate, then wait, then move on.
        assert "terminate" in events
        assert any(isinstance(e, tuple) and e[0] == "wait" for e in events)
        # No second shutdown call should fail.
        server.shutdown()


class TestInputProxyRouting:
    """When `input_proxy` is set, the five input ops (click / key /
    type / scroll / activate) MUST route to the remote backend; capture
    ops MUST stay on the local SikuliX bridge. This is the heart of
    the split-input-proxy topology — getting it wrong silently means
    input ops fire on the wrong machine."""

    def _make_server_with_proxy(self) -> tuple[HoloMCPServer, _StubBridge, Any]:
        """Build a HoloMCPServer with a fake bridge AND a fake remote-input
        attached on the daemon, so we can assert which one each tool
        method targets."""
        bridge = _StubBridge()
        remote = _StubBridge()  # same API, different recorder

        server = HoloMCPServer()
        fake = _FakeDaemon(bridge=bridge)
        fake._remote_input = remote  # populate the routing target
        server._daemon = fake
        return server, bridge, remote

    def test_screen_click_routes_to_remote_when_proxy_set(self):
        server, bridge, remote = self._make_server_with_proxy()
        server.screen_click(100, 200)
        assert any(call[0] == "click" for call in remote.calls)
        assert not any(call[0] == "click" for call in bridge.calls)

    def test_screen_key_routes_to_remote_when_proxy_set(self):
        server, bridge, remote = self._make_server_with_proxy()
        server.screen_key("cmd+s")
        assert any(call[0] == "key" for call in remote.calls)
        assert not any(call[0] == "key" for call in bridge.calls)

    def test_screen_type_routes_to_remote_when_proxy_set(self):
        server, bridge, remote = self._make_server_with_proxy()
        server.screen_type("hello")
        assert any(call[0] == "type_text" for call in remote.calls)
        assert not any(call[0] == "type_text" for call in bridge.calls)

    def test_screen_scroll_routes_to_remote_when_proxy_set(self):
        server, bridge, remote = self._make_server_with_proxy()
        server.screen_scroll(10, 20, direction="up", steps=5)
        assert any(call[0] == "scroll" for call in remote.calls)
        assert not any(call[0] == "scroll" for call in bridge.calls)

    def test_screen_move_routes_to_remote_when_proxy_set(self):
        server, bridge, remote = self._make_server_with_proxy()
        server.screen_move(123, 456)
        assert any(call[0] == "mouse_move" for call in remote.calls)
        assert not any(call[0] == "mouse_move" for call in bridge.calls)

    def test_app_activate_routes_to_remote_when_proxy_set(self):
        server, bridge, remote = self._make_server_with_proxy()
        server.app_activate("Google Chrome")
        assert any(call[0] == "activate" for call in remote.calls)
        assert not any(call[0] == "activate" for call in bridge.calls)

    def test_screen_shot_stays_local_when_proxy_set(self):
        """Capture MUST remain local — sending pixels over the wire on
        every screenshot would be slow and defeats the whole point of
        the topology (capture works locally, only input is blocked)."""
        server, bridge, remote = self._make_server_with_proxy()
        server.screen_shot()
        assert any(call[0] == "screenshot" for call in bridge.calls)
        assert not any(call[0] == "screenshot" for call in remote.calls)

    def test_ui_template_click_uses_remote_input_after_local_find(self, tmp_path):
        """`ui_template_click` is a compound: find locally on this host,
        then click via the input target. With `input_proxy` set, the
        click should fire on the remote, but the find / screenshot
        underneath stays local."""
        import struct
        import zlib

        from holo.templates import TemplateStore

        def _make_png(w: int, h: int) -> bytes:
            sig = b"\x89PNG\r\n\x1a\n"
            def _chunk(typ: bytes, data: bytes) -> bytes:
                crc = zlib.crc32(typ + data) & 0xFFFFFFFF
                return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)
            ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
            raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
            return (
                sig + _chunk(b"IHDR", ihdr)
                + _chunk(b"IDAT", zlib.compress(raw))
                + _chunk(b"IEND", b"")
            )

        store = TemplateStore(root=tmp_path / "templates")
        bridge = _StubBridge()
        remote = _StubBridge()
        # Pre-populate a template so find succeeds.
        store.add_variant("save_button", "chrome", _make_png(8, 8), similarity=0.85)

        server = HoloMCPServer(templates=store)
        fake = _FakeDaemon(bridge=bridge)
        fake._remote_input = remote
        server._daemon = fake

        out = server.ui_template_click("save_button", app="chrome")
        assert out["clicked"] is True

        # find_image_path runs on the local bridge…
        assert any(call[0] == "find_image_path" for call in bridge.calls)
        # …click fires on the remote.
        assert any(call[0] == "click" for call in remote.calls)
        assert not any(call[0] == "click" for call in bridge.calls)

    def test_no_proxy_routes_to_local_bridge(self):
        """Sanity / regression: without `input_proxy`, every op stays on
        the local bridge."""
        bridge = _StubBridge()
        server = HoloMCPServer()
        fake = _FakeDaemon(bridge=bridge)
        fake._remote_input = None
        server._daemon = fake

        server.screen_click(1, 2)
        server.screen_key("enter")
        server.screen_type("x")
        server.app_activate("Finder")
        server.screen_move(50, 60)
        bridge_methods = [call[0] for call in bridge.calls]
        assert "click" in bridge_methods
        assert "key" in bridge_methods
        assert "type_text" in bridge_methods
        assert "activate" in bridge_methods
        assert "mouse_move" in bridge_methods


class TestRemoteScreenRouting:
    """Symmetric to TestInputProxyRouting. When `remote_screen` is set,
    the capture ops (screen_shot / screen_find_image / screen_user_capture
    / ui_template_find / ui_template_capture) MUST route to the remote;
    input ops (click / key / type / scroll / activate) MUST stay local.

    `ui_template_click` is the cross-cutting compound: find goes to the
    capture target, click goes to the input target — they can target
    different machines independently."""

    def _make_server_with_screen_proxy(self) -> tuple[HoloMCPServer, _StubBridge, Any]:
        bridge = _StubBridge()
        remote = _StubBridge()
        server = HoloMCPServer()
        fake = _FakeDaemon(bridge=bridge)
        fake._remote_screen = remote
        server._daemon = fake
        return server, bridge, remote

    def test_screen_shot_routes_to_remote_when_proxy_set(self):
        server, bridge, remote = self._make_server_with_screen_proxy()
        server.screen_shot()
        assert any(call[0] == "screenshot" for call in remote.calls)
        assert not any(call[0] == "screenshot" for call in bridge.calls)

    def test_screen_find_image_routes_to_remote_when_proxy_set(self):
        import base64 as _b64
        server, bridge, remote = self._make_server_with_screen_proxy()
        needle_b64 = _b64.b64encode(b"\x89PNGfake").decode("ascii")
        server.screen_find_image(needle_b64)
        assert any(call[0] == "find_image" for call in remote.calls)
        assert not any(call[0] == "find_image" for call in bridge.calls)

    def test_screen_user_capture_routes_to_remote_when_proxy_set(self):
        server, bridge, remote = self._make_server_with_screen_proxy()
        server.screen_user_capture(prompt="select me")
        assert any(call[0] == "user_capture" for call in remote.calls)
        assert not any(call[0] == "user_capture" for call in bridge.calls)

    def test_screen_click_stays_local_when_only_screen_proxy_set(self):
        """`--remote-screen` MUST NOT affect input — clicks fire on the
        local bridge unless `--input-proxy` is ALSO set. Same the other
        way around."""
        server, bridge, remote = self._make_server_with_screen_proxy()
        server.screen_click(1, 2)
        assert any(call[0] == "click" for call in bridge.calls)
        assert not any(call[0] == "click" for call in remote.calls)

    def test_ui_template_find_uses_capture_target(self, tmp_path):
        """The find inside ui_template_find should hit the remote when
        remote_screen is set (the template file is read locally and the
        bytes are sent to the remote for matching against its screen)."""
        import struct
        import zlib

        from holo.templates import TemplateStore

        def _make_png(w: int, h: int) -> bytes:
            sig = b"\x89PNG\r\n\x1a\n"
            def _chunk(typ: bytes, data: bytes) -> bytes:
                crc = zlib.crc32(typ + data) & 0xFFFFFFFF
                return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)
            ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
            raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
            return (
                sig
                + _chunk(b"IHDR", ihdr)
                + _chunk(b"IDAT", zlib.compress(raw))
                + _chunk(b"IEND", b"")
            )

        store = TemplateStore(root=tmp_path / "templates")
        store.add_variant("kebab", "chrome", _make_png(8, 8), similarity=0.85)
        bridge = _StubBridge()
        remote = _StubBridge()
        server = HoloMCPServer(templates=store)
        fake = _FakeDaemon(bridge=bridge)
        fake._remote_screen = remote
        server._daemon = fake

        server.ui_template_find("kebab", app="chrome")

        # find_image_path went to the remote, NOT the local bridge.
        assert any(call[0] == "find_image_path" for call in remote.calls)
        assert not any(call[0] == "find_image_path" for call in bridge.calls)

    def test_ui_template_click_splits_capture_and_input_across_remotes(self, tmp_path):
        """The big integration assertion: with both proxies set, find
        runs on the screen target and click runs on the input target.
        If they're different machines, BOTH must see exactly one of
        their respective ops."""
        import struct
        import zlib

        from holo.templates import TemplateStore

        def _make_png(w: int, h: int) -> bytes:
            sig = b"\x89PNG\r\n\x1a\n"
            def _chunk(typ: bytes, data: bytes) -> bytes:
                crc = zlib.crc32(typ + data) & 0xFFFFFFFF
                return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)
            ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
            raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
            return (
                sig
                + _chunk(b"IHDR", ihdr)
                + _chunk(b"IDAT", zlib.compress(raw))
                + _chunk(b"IEND", b"")
            )

        store = TemplateStore(root=tmp_path / "templates")
        store.add_variant("kebab", "chrome", _make_png(8, 8), similarity=0.85)
        bridge = _StubBridge()
        input_remote = _StubBridge()
        screen_remote = _StubBridge()
        server = HoloMCPServer(templates=store)
        fake = _FakeDaemon(bridge=bridge)
        fake._remote_input = input_remote
        fake._remote_screen = screen_remote
        server._daemon = fake

        out = server.ui_template_click("kebab", app="chrome")
        assert out["clicked"] is True

        # find on the screen target, click on the input target — local
        # bridge gets neither.
        assert any(c[0] == "find_image_path" for c in screen_remote.calls)
        assert not any(c[0] == "find_image_path" for c in input_remote.calls)
        assert not any(c[0] == "find_image_path" for c in bridge.calls)
        assert any(c[0] == "click" for c in input_remote.calls)
        assert not any(c[0] == "click" for c in screen_remote.calls)
        assert not any(c[0] == "click" for c in bridge.calls)


class TestDaemonBackendSharing:
    """When `input_proxy == remote_screen` (same host:port), the Daemon
    constructs ONE RemoteHoloBackend and assigns it to both
    `_remote_input` and `_remote_screen` — saves a connect subprocess +
    a JVM-init-worth of latency. When they differ, two backends. The
    test checks both code paths use the same RemoteHoloBackend
    constructor injected via monkeypatch — no real network."""

    def _fake_backend(self):
        class _FakeBackend:
            instances: list[Any] = []
            def __init__(self, host, port):
                self.host = host
                self.port = port
                self.started = False
                self.stopped = False
                _FakeBackend.instances.append(self)
            def start(self):
                self.started = True
            def stop(self):
                self.stopped = True
        return _FakeBackend

    def test_same_host_port_shares_one_backend(self, monkeypatch):
        from holo import remote_backend
        from holo.daemon import Daemon

        Fake = self._fake_backend()
        monkeypatch.setattr(remote_backend, "RemoteHoloBackend", Fake)

        d = Daemon(
            no_bookmarklet=True,
            input_proxy=("host", 7081),
            remote_screen=("host", 7081),
        )
        try:
            assert len(Fake.instances) == 1
            assert d._remote_input is d._remote_screen is Fake.instances[0]
        finally:
            d.shutdown()
        # stop called exactly once on the shared instance.
        assert Fake.instances[0].stopped is True

    def test_different_host_port_creates_two_backends(self, monkeypatch):
        from holo import remote_backend
        from holo.daemon import Daemon

        Fake = self._fake_backend()
        monkeypatch.setattr(remote_backend, "RemoteHoloBackend", Fake)

        d = Daemon(
            no_bookmarklet=True,
            input_proxy=("input-host", 7081),
            remote_screen=("screen-host", 7081),
        )
        try:
            assert len(Fake.instances) == 2
            assert d._remote_input is not d._remote_screen
            assert d._remote_input.host == "input-host"
            assert d._remote_screen.host == "screen-host"
        finally:
            d.shutdown()


class TestScreenTools:
    """SikuliX-backed tools that don't take a sid — they drive whatever's
    in the foreground. Each delegates straight to `daemon.bridge`; the
    server is responsible only for the bridge-availability check and
    base64 marshalling."""

    @pytest.fixture
    def server_with_bridge(self):
        bridge = _StubBridge()
        server = HoloMCPServer(enable_screen=True)
        fake = _FakeDaemon(bridge=bridge)
        server._daemon = fake  # bypass lazy init
        return server, bridge

    def test_app_activate_delegates(self, server_with_bridge):
        server, bridge = server_with_bridge
        out = server.app_activate("Google Chrome")
        assert out == {"focused": True, "name": "Google Chrome"}
        assert bridge.calls == [("activate", {"name": "Google Chrome"})]

    def test_screen_click_passes_modifiers(self, server_with_bridge):
        server, bridge = server_with_bridge
        server.screen_click(100, 200, modifiers=["cmd"])
        assert bridge.calls == [
            ("click", {"x": 100, "y": 200, "modifiers": ["cmd"]})
        ]

    def test_screen_type_and_key(self, server_with_bridge):
        server, bridge = server_with_bridge
        server.screen_type("hello")
        server.screen_key("cmd+v")
        assert bridge.calls == [
            ("type_text", {"text": "hello"}),
            ("key", {"combo": "cmd+v"}),
        ]

    def test_screen_shot_returns_base64_payload(self, server_with_bridge):
        import base64

        server, bridge = server_with_bridge
        bridge.next_screenshot = b"PNG-bytes-here"
        out = server.screen_shot()
        assert out["format"] == "png"
        assert out["byte_count"] == len(b"PNG-bytes-here")
        assert base64.b64decode(out["image"]) == b"PNG-bytes-here"
        assert bridge.calls == [("screenshot", {"region": None})]

    def test_screen_shot_passes_region(self, server_with_bridge):
        server, bridge = server_with_bridge
        region = {"x": 10, "y": 20, "width": 30, "height": 40}
        server.screen_shot(region=region)
        assert bridge.calls[-1] == ("screenshot", {"region": region})

    def test_screen_scroll_defaults_to_down_three_steps(self, server_with_bridge):
        server, bridge = server_with_bridge
        out = server.screen_scroll(100, 200)
        assert out["scrolled"] is True
        assert bridge.calls[-1] == (
            "scroll",
            {"x": 100, "y": 200, "direction": "down", "steps": 3},
        )

    def test_screen_scroll_passes_explicit_args(self, server_with_bridge):
        server, bridge = server_with_bridge
        server.screen_scroll(50, 60, direction="up", steps=10)
        assert bridge.calls[-1] == (
            "scroll",
            {"x": 50, "y": 60, "direction": "up", "steps": 10},
        )

    def test_screen_move_calls_bridge_mouse_move(self, server_with_bridge):
        server, bridge = server_with_bridge
        out = server.screen_move(333, 444)
        assert out == {"moved": True, "x": 333, "y": 444}
        assert bridge.calls[-1] == ("mouse_move", {"x": 333, "y": 444})

    def test_screen_find_image_decodes_needle(self, server_with_bridge):
        import base64

        server, bridge = server_with_bridge
        needle_bytes = b"\x89PNG-needle-bytes"
        out = server.screen_find_image(
            base64.b64encode(needle_bytes).decode("ascii"), score=0.9
        )
        assert out == {"x": 50, "y": 60, "width": 30, "height": 30, "score": 0.95}
        assert bridge.calls[-1][0] == "find_image"
        params = bridge.calls[-1][1]
        assert params["needle"] == needle_bytes
        assert params["score"] == 0.9
        assert params["region"] is None

    def test_screen_find_image_returns_none_for_no_match(self, server_with_bridge):
        import base64

        server, bridge = server_with_bridge
        bridge.next_find = None
        out = server.screen_find_image(base64.b64encode(b"x").decode())
        assert out is None

    def test_screen_find_image_rejects_bad_base64(self, server_with_bridge):
        server, _ = server_with_bridge
        with pytest.raises(ValueError, match="base64"):
            server.screen_find_image("!!!not-base64!!!")

    def test_no_bridge_raises_clean_error(self):
        server = HoloMCPServer(enable_screen=False)
        fake = _FakeDaemon(bridge=None)
        server._daemon = fake
        with pytest.raises(RuntimeError, match="Screen tools unavailable"):
            server.screen_click(0, 0)


class TestBrowserTools:
    """The browser_* MCP tools wrap `holo.browser_chrome`. They don't
    touch the daemon or the bridge — they shell out to osascript. Here
    we verify the MCP layer's error translation; the AppleScript
    snippets and parsing are covered in `test_browser_chrome.py`.
    """

    def _server_no_daemon(self) -> HoloMCPServer:
        # Browser tools don't touch the daemon, but we don't want lazy
        # construction to start a real Daemon mid-test if something
        # accidentally pokes it.
        server = HoloMCPServer()
        server._daemon = _FakeDaemon()
        return server

    def test_browser_navigate_delegates(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.navigate") as nav:
            nav.return_value = {"url": "https://x/"}
            out = server.browser_navigate("https://x/")

        assert out == {"url": "https://x/"}
        nav.assert_called_once_with("https://x/")

    def test_browser_navigate_translates_browser_error_to_runtime_error(self):
        from unittest.mock import patch

        from holo.browser_chrome import BrowserError

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.navigate", side_effect=BrowserError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                server.browser_navigate("https://x/")

    def test_browser_navigate_translates_not_available(self):
        from unittest.mock import patch

        from holo.browser_chrome import BrowserNotAvailable

        server = self._server_no_daemon()
        with patch(
            "holo.browser_chrome.navigate",
            side_effect=BrowserNotAvailable("linux"),
        ):
            with pytest.raises(RuntimeError, match="linux"):
                server.browser_navigate("https://x/")

    def test_browser_list_tabs_passthrough(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        payload = {
            "tabs": [{"id": 1, "title": "x", "url": "https://x/", "index": 1}],
            "active": 1,
        }
        with patch("holo.browser_chrome.list_tabs", return_value=payload):
            assert server.browser_list_tabs() == payload

    def test_browser_new_tab_with_and_without_url(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.new_tab") as new_tab:
            new_tab.return_value = {"url": "https://y/"}
            server.browser_new_tab("https://y/")
            new_tab.assert_called_once_with("https://y/")

            new_tab.reset_mock()
            new_tab.return_value = {"url": "chrome://newtab/"}
            server.browser_new_tab()
            new_tab.assert_called_once_with(None)

    def test_browser_activate_tab_passes_index(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.activate_tab") as act:
            act.return_value = {"index": 4}
            assert server.browser_activate_tab(4) == {"index": 4}
            act.assert_called_once_with(4)

    def test_browser_execute_js_delegates(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.execute_js") as exec_js:
            exec_js.return_value = {"result": "Click me"}
            out = server.browser_execute_js(
                "document.querySelector('button')?.innerText"
            )
        assert out == {"result": "Click me"}
        exec_js.assert_called_once_with(
            "document.querySelector('button')?.innerText"
        )

    def test_browser_execute_js_surfaces_authorization_message(self):
        """When Chrome's JS-from-AppleEvents is off, the agent should
        see a message that names the menu item AND points at
        bookmarklet_query as the fallback."""
        from unittest.mock import patch

        from holo.browser_chrome import JavaScriptNotAuthorized

        server = self._server_no_daemon()
        with patch(
            "holo.browser_chrome.execute_js",
            side_effect=JavaScriptNotAuthorized(
                "Chrome's 'Allow JavaScript from Apple Events' is off..."
            ),
        ):
            with pytest.raises(RuntimeError, match="Allow JavaScript from Apple Events"):
                server.browser_execute_js("document.title")


class TestBookmarkletQuery:
    """`bookmarklet_query` rides on the existing channel send_command
    path — we just verify it builds the right command shape."""

    @pytest.fixture
    def server_with_channel(self):
        server = HoloMCPServer()
        fake = _FakeDaemon()
        server._daemon = fake
        ch = _StubChannel("sid-A", replies=[{"value": "Click me"}])
        fake.registry.register("sid-A", ch)
        return server, ch

    def test_default_uses_query_selector_and_innerText(self, server_with_channel):
        server, ch = server_with_channel
        out = server.bookmarklet_query("sid-A", "button")
        assert out["result"] == {"value": "Click me"}
        cmd, _timeout = ch.calls[-1]
        assert cmd == {"op": "query_selector", "selector": "button", "prop": "innerText"}

    def test_all_flag_switches_to_query_selector_all(self, server_with_channel):
        server, ch = server_with_channel
        # The stub returns the canned reply regardless of op.
        server.bookmarklet_query("sid-A", "button", all=True)
        cmd, _ = ch.calls[-1]
        assert cmd["op"] == "query_selector_all"

    def test_attr_takes_precedence_over_prop(self, server_with_channel):
        server, ch = server_with_channel
        server.bookmarklet_query("sid-A", "a", prop="innerText", attr="href")
        cmd, _ = ch.calls[-1]
        assert "attr" in cmd and cmd["attr"] == "href"
        assert "prop" not in cmd

    def test_custom_prop(self, server_with_channel):
        server, ch = server_with_channel
        server.bookmarklet_query("sid-A", "h1", prop="innerHTML")
        cmd, _ = ch.calls[-1]
        assert cmd["prop"] == "innerHTML"

    def test_rejects_empty_selector(self, server_with_channel):
        server, _ = server_with_channel
        with pytest.raises(ValueError, match="selector"):
            server.bookmarklet_query("sid-A", "")

    def test_unknown_sid_raises(self, server_with_channel):
        server, _ = server_with_channel
        with pytest.raises(ValueError, match="no channel"):
            server.bookmarklet_query("nope", "button")


class TestUiTemplates:
    """Template cache MCP tools — capture / list / find / click / delete.

    The TemplateStore itself is exercised exhaustively in test_templates.py;
    here we just verify the MCP layer routes correctly to the store and
    bridge, and that find/click integrate them properly.
    """

    @pytest.fixture
    def fixtures(self, tmp_path):
        """Server wired to a stubbed bridge + a tmp-dir TemplateStore."""
        from holo.templates import TemplateStore

        store = TemplateStore(root=tmp_path / "templates")
        bridge = _StubBridge()
        # 24x24 PNG that the bridge "captured" and that find_image_path "matches".
        png = self._make_png(24, 24)
        bridge.next_screenshot = png
        bridge.next_capture = {
            "image": __import__("base64").b64encode(png).decode(),
            "x": 100, "y": 200, "width": 24, "height": 24,
        }
        bridge.next_find = {
            "x": 100, "y": 200, "width": 24, "height": 24, "score": 0.91,
        }
        server = HoloMCPServer(templates=store)
        server._daemon = _FakeDaemon(bridge=bridge)
        return server, bridge, store, png

    @staticmethod
    def _make_png(w, h):
        import struct
        import zlib

        sig = b"\x89PNG\r\n\x1a\n"

        def chunk(typ, data):
            crc = zlib.crc32(typ + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)

        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
        idat = zlib.compress(raw)
        return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

    # ---- capture --------------------------------------------------------

    def test_capture_with_region_uses_screenshot(self, fixtures):
        server, bridge, store, _ = fixtures
        out = server.ui_template_capture(
            "kebab", app="chrome",
            region={"x": 0, "y": 0, "width": 24, "height": 24},
        )
        # The bridge was asked for a screenshot, not a userCapture.
        assert any(c[0] == "screenshot" for c in bridge.calls)
        assert not any(c[0] == "user_capture" for c in bridge.calls)
        assert out["saved"] is True
        assert out["entry"]["app"] == "chrome"
        assert out["entry"]["label"] == "kebab"
        assert store.get("kebab", "chrome") is not None

    def test_capture_without_region_calls_user_capture(self, fixtures):
        server, bridge, store, _ = fixtures
        out = server.ui_template_capture("kebab", app="chrome")
        assert any(c[0] == "user_capture" for c in bridge.calls)
        assert out["saved"] is True
        assert store.get("kebab", "chrome") is not None

    def test_capture_cancelled_returns_cancelled_marker_no_save(self, fixtures):
        server, bridge, store, _ = fixtures
        bridge.next_capture = {"cancelled": True, "reason": "user cancelled"}
        out = server.ui_template_capture("kebab", app="chrome")
        assert out == {"cancelled": True, "reason": "user cancelled"}
        # Nothing was written to the cache.
        assert store.get("kebab", "chrome") is None

    def test_capture_propagates_replace_and_similarity(self, fixtures):
        server, _, store, _ = fixtures
        server.ui_template_capture(
            "kebab", app="chrome",
            region={"x": 0, "y": 0, "width": 24, "height": 24},
        )
        server.ui_template_capture(
            "kebab", app="chrome",
            region={"x": 0, "y": 0, "width": 24, "height": 24},
            replace=True,
            similarity=0.95,
        )
        entry = store.get("kebab", "chrome")
        assert entry["variants"] == ["kebab.png"]
        assert entry["similarity"] == 0.95

    # ---- list -----------------------------------------------------------

    def test_list_filters_by_app(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("a", "chrome", png)
        store.add_variant("b", "slack", png)
        out = server.ui_template_list(app="chrome")
        assert [e["label"] for e in out["templates"]] == ["a"]

    def test_list_all(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("a", "chrome", png)
        store.add_variant("b", None, png)
        out = server.ui_template_list()
        assert len(out["templates"]) == 2

    # ---- find -----------------------------------------------------------

    def test_find_returns_match_with_variant_name(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png, similarity=0.9)
        out = server.ui_template_find("kebab", app="chrome")
        assert out["score"] == 0.91
        assert out["variant"] == "kebab.png"
        assert out["x"] == 100
        # Bridge was asked with the entry's similarity, not the default.
        path_calls = [c for c in bridge.calls if c[0] == "find_image_path"]
        assert path_calls and path_calls[-1][1]["score"] == 0.9

    def test_find_walks_variants_in_order(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        store.add_variant("kebab", "chrome", png)  # _2
        # First variant misses, second hits.
        responses = [None, {
            "x": 1, "y": 2, "width": 24, "height": 24, "score": 0.88,
        }]

        def stepwise_find(path, *, region=None, score=0.7, timeout=15.0):
            bridge.calls.append(("find_image_path", {"path": str(path)}))
            return responses.pop(0)

        bridge.find_image_path = stepwise_find  # type: ignore[assignment]
        out = server.ui_template_find("kebab", app="chrome")
        assert out["variant"] == "kebab_2.png"

    def test_find_returns_null_when_no_variant_matches(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        bridge.next_find = None
        assert server.ui_template_find("kebab", app="chrome") is None

    def test_find_raises_lookup_error_for_missing_label(self, fixtures):
        server, _, _, _ = fixtures
        with pytest.raises(LookupError, match="kebab"):
            server.ui_template_find("kebab", app="chrome")

    def test_find_bumps_last_used_and_match_count(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        server.ui_template_find("kebab", app="chrome")
        entry = store.get("kebab", "chrome")
        assert entry["match_count"] == 1
        assert entry["last_used"] is not None

    # ---- click ----------------------------------------------------------

    def test_click_finds_and_clicks_center(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        out = server.ui_template_click("kebab", app="chrome")
        # Center of the 100,200 / 24x24 match is (112, 212).
        assert out == {
            "clicked": True, "x": 112, "y": 212, "score": 0.91, "variant": "kebab.png"
        }
        assert ("click", {"x": 112, "y": 212, "modifiers": []}) in bridge.calls

    def test_click_raises_when_template_doesnt_match(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        bridge.next_find = None
        with pytest.raises(RuntimeError, match="matched nothing"):
            server.ui_template_click("kebab", app="chrome")
        # No click was issued.
        assert not any(c[0] == "click" for c in bridge.calls)

    def test_click_raises_for_missing_label(self, fixtures):
        server, _, _, _ = fixtures
        with pytest.raises(LookupError, match="kebab"):
            server.ui_template_click("kebab", app="chrome")

    # ---- delete ---------------------------------------------------------

    def test_delete_removes_entry(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        out = server.ui_template_delete("kebab", app="chrome")
        assert "kebab.png" in out["removed"]
        assert store.get("kebab", "chrome") is None

    def test_delete_one_variant_keeps_entry(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        store.add_variant("kebab", "chrome", png)
        out = server.ui_template_delete("kebab", app="chrome", variant="kebab.png")
        assert out["removed"] == ["kebab.png"]
        entry = store.get("kebab", "chrome")
        assert entry["variants"] == ["kebab_2.png"]


# ============================================================================
# LAN session discovery / capabilities
# ============================================================================


def _fake_session(
    *,
    instance: str = "holo-host-12345-abcdef",
    host: str = "host.local",
    session: str | None = None,
    ips: list[str] | None = None,
    caps_port: int | None = None,
    caps_token: str | None = None,
) -> dict[str, Any]:
    """Build a discover-shaped session dict for tests."""
    s: dict[str, Any] = {
        "instance": instance,
        "host": host,
        "user": "alice",
        "v": "1",
        "holo_pid": 1234,
        "holo_version": "0.1.0a15",
        "started": 1_700_000_000,
        "cwd": "/home/alice",
        "last_seen": 1_700_000_001,
    }
    if session is not None:
        s["session"] = session
    if ips is not None:
        s["ips"] = ips
    if caps_port is not None:
        s["caps_port"] = caps_port
    if caps_token is not None:
        s["caps_token"] = caps_token
    return s


class TestMatchSession:
    """`_match_session` priority: instance > session > host."""

    def test_matches_by_instance(self) -> None:
        from holo.mcp_server import _match_session

        sessions = [
            _fake_session(instance="holo-x-1-aaa", session="x"),
            _fake_session(instance="holo-y-2-bbb", session="y"),
        ]
        assert _match_session(sessions, "holo-x-1-aaa")["session"] == "x"

    def test_matches_by_session_when_no_instance_hit(self) -> None:
        from holo.mcp_server import _match_session

        sessions = [_fake_session(instance="holo-x-1-aaa", session="claude-1")]
        assert _match_session(sessions, "claude-1")["instance"] == "holo-x-1-aaa"

    def test_matches_by_host_as_last_resort(self) -> None:
        from holo.mcp_server import _match_session

        sessions = [_fake_session(instance="holo-x-1-aaa", host="m4-pro.local")]
        assert _match_session(sessions, "m4-pro.local") is sessions[0]

    def test_instance_beats_session_collision(self) -> None:
        from holo.mcp_server import _match_session

        # Two sessions with the same `session` value; pass the unique
        # instance label and we should get exactly that one back.
        sessions = [
            _fake_session(instance="holo-a-1-aaa", session="dup"),
            _fake_session(instance="holo-b-2-bbb", session="dup"),
        ]
        out = _match_session(sessions, "holo-b-2-bbb")
        assert out["instance"] == "holo-b-2-bbb"

    def test_no_match_returns_none(self) -> None:
        from holo.mcp_server import _match_session

        sessions = [_fake_session(instance="holo-x-1-aaa")]
        assert _match_session(sessions, "ghost") is None


class TestHoloDiscoverSessions:
    """`holo_discover_sessions` wraps `_start_browser` + a wait."""

    def test_returns_snapshot_shape(self) -> None:
        # Patch `_start_browser` to yield a fake store with two sessions.
        from holo.discover import SessionStore

        store = SessionStore()
        store.upsert(_fake_session(instance="holo-a-1-aaa", session="alpha"))
        store.upsert(_fake_session(instance="holo-b-2-bbb", session="beta"))

        fake_zc: Any = type("ZC", (), {"close": lambda self: None})()

        from unittest.mock import patch

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(fake_zc, None, store),
            ),
            patch("time.sleep"),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                out = server.holo_discover_sessions(wait_s=0.0)
            finally:
                server.shutdown()

        assert out["count"] == 2
        instances = {s["instance"] for s in out["sessions"]}
        assert instances == {"holo-a-1-aaa", "holo-b-2-bbb"}

    def test_browser_closed_on_return(self) -> None:
        from holo.discover import SessionStore

        store = SessionStore()
        closed: dict[str, bool] = {"yes": False}

        class FakeZC:
            def close(self) -> None:
                closed["yes"] = True

        from unittest.mock import patch

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(FakeZC(), None, store),
            ),
            patch("time.sleep"),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                server.holo_discover_sessions(wait_s=0.0)
            finally:
                server.shutdown()
        assert closed["yes"] is True


class TestHoloFetchCapabilities:
    """End-to-end: discover → match → HTTP fetch with auth header."""

    def _setup_with_session(
        self, target: dict[str, Any]
    ) -> tuple[Any, dict[str, bool]]:
        from holo.discover import SessionStore

        store = SessionStore()
        store.upsert(target)
        closed: dict[str, bool] = {"closed": False}

        class FakeZC:
            def close(self) -> None:
                closed["closed"] = True

        return (FakeZC(), store), closed

    def test_happy_path_returns_capabilities(self) -> None:
        import io
        from unittest.mock import patch

        target = _fake_session(
            instance="holo-x-1-aaa",
            ips=["10.0.0.5"],
            caps_port=49597,
            caps_token="tok",
        )
        (zc, store), _ = self._setup_with_session(target)

        caps_body = {
            "schema": 1,
            "host": {"os": "darwin", "arch": "arm64"},
        }

        captured_req = {"req": None}

        def fake_urlopen(req: Any, timeout: float = 0) -> Any:
            captured_req["req"] = req
            resp = io.BytesIO(b'{"schema":1,"host":{"os":"darwin","arch":"arm64"}}')
            resp.__enter__ = lambda self: self  # type: ignore[attr-defined]
            resp.__exit__ = lambda self, *a: None  # type: ignore[attr-defined]
            return resp

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(zc, None, store),
            ),
            patch("time.sleep"),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                out = server.holo_fetch_capabilities("holo-x-1-aaa")
            finally:
                server.shutdown()

        assert out["instance"] == "holo-x-1-aaa"
        assert out["ip_used"] == "10.0.0.5"
        assert out["capabilities"] == caps_body
        # Auth header on the request.
        req = captured_req["req"]
        assert req is not None
        # Header lookup is case-insensitive in urllib's mapping.
        assert req.get_header("X-holo-caps-token") == "tok"

    def test_session_not_found(self) -> None:
        from unittest.mock import patch

        from holo.discover import SessionStore

        store = SessionStore()
        # No sessions registered.

        class FakeZC:
            def close(self) -> None:
                pass

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(FakeZC(), None, store),
            ),
            patch("time.sleep"),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                with pytest.raises(RuntimeError, match="no holo session matching"):
                    server.holo_fetch_capabilities("ghost")
            finally:
                server.shutdown()

    def test_session_without_caps_endpoint_errors(self) -> None:
        from unittest.mock import patch

        target = _fake_session(
            instance="holo-x-1-aaa", ips=["10.0.0.5"]
        )  # no caps_port / caps_token
        (zc, store), _ = self._setup_with_session(target)

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(zc, None, store),
            ),
            patch("time.sleep"),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                with pytest.raises(RuntimeError, match="no capabilities endpoint"):
                    server.holo_fetch_capabilities("holo-x-1-aaa")
            finally:
                server.shutdown()

    def test_falls_through_unreachable_ips_to_reachable_one(self) -> None:
        """The motivating field issue: VPN tunnel address ahead of LAN
        address. First IP times out, second succeeds; we report the
        second."""
        import io
        from unittest.mock import patch

        target = _fake_session(
            instance="holo-x-1-aaa",
            ips=["192.168.193.226", "192.168.1.111"],
            caps_port=49597,
            caps_token="tok",
        )
        (zc, store), _ = self._setup_with_session(target)

        attempts: list[str] = []

        def fake_urlopen(req: Any, timeout: float = 0) -> Any:
            url = req.full_url
            attempts.append(url)
            # First IP raises; second returns a body.
            if "192.168.193.226" in url:
                raise OSError("connect timeout")
            resp = io.BytesIO(b'{"schema":1,"host":{}}')
            resp.__enter__ = lambda self: self  # type: ignore[attr-defined]
            resp.__exit__ = lambda self, *a: None  # type: ignore[attr-defined]
            return resp

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(zc, None, store),
            ),
            patch("time.sleep"),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                out = server.holo_fetch_capabilities("holo-x-1-aaa")
            finally:
                server.shutdown()

        assert len(attempts) == 2
        assert out["ip_used"] == "192.168.1.111"

    def test_all_ips_unreachable_errors_with_attempts(self) -> None:
        from unittest.mock import patch

        target = _fake_session(
            instance="holo-x-1-aaa",
            ips=["10.0.0.5", "10.0.0.6"],
            caps_port=49597,
            caps_token="tok",
        )
        (zc, store), _ = self._setup_with_session(target)

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(zc, None, store),
            ),
            patch("time.sleep"),
            patch(
                "urllib.request.urlopen",
                side_effect=OSError("no route"),
            ),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                with pytest.raises(
                    RuntimeError, match="could not reach capabilities endpoint"
                ) as ei:
                    server.holo_fetch_capabilities("holo-x-1-aaa")
            finally:
                server.shutdown()
        # Error message names every IP we tried.
        assert "10.0.0.5" in str(ei.value)
        assert "10.0.0.6" in str(ei.value)


class TestPersistentDiscoverCache:
    """The persistent DiscoverHandle should be created once at __init__
    and reused across every tool call — no per-request mDNS browse."""

    def test_start_browser_called_exactly_once_for_many_calls(self) -> None:
        from holo.discover import SessionStore

        store = SessionStore()
        store.upsert(
            _fake_session(
                instance="holo-x-1-aaa",
                ips=["10.0.0.5"],
                caps_port=49597,
                caps_token="tok",
            )
        )

        class FakeZC:
            def close(self) -> None:
                pass

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(FakeZC(), None, store),
            ) as start_browser,
            patch(
                "holo.discover._start_stale_sweeper",
                return_value=MagicMock(),
            ),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                # Ten back-to-back queries.
                for _ in range(10):
                    server.holo_discover_sessions()
            finally:
                server.shutdown()
            # __init__ called start once; the ten tool calls didn't
            # add to the count.
            assert start_browser.call_count == 1

    def test_fetch_capabilities_does_not_re_browse(self) -> None:
        import io

        from holo.discover import SessionStore

        store = SessionStore()
        store.upsert(
            _fake_session(
                instance="holo-x-1-aaa",
                ips=["10.0.0.5"],
                caps_port=49597,
                caps_token="tok",
            )
        )

        class FakeZC:
            def close(self) -> None:
                pass

        def fake_urlopen(req: Any, timeout: float = 0) -> Any:
            resp = io.BytesIO(b'{"schema":1,"host":{}}')
            resp.__enter__ = lambda self: self  # type: ignore[attr-defined]
            resp.__exit__ = lambda self, *a: None  # type: ignore[attr-defined]
            return resp

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(FakeZC(), None, store),
            ) as start_browser,
            patch(
                "holo.discover._start_stale_sweeper",
                return_value=MagicMock(),
            ),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                server.holo_fetch_capabilities("holo-x-1-aaa")
                server.holo_fetch_capabilities("holo-x-1-aaa")
                server.holo_fetch_capabilities("holo-x-1-aaa")
            finally:
                server.shutdown()
            # One browser, ever — three fetches did NOT spawn new
            # zeroconf instances.
            assert start_browser.call_count == 1

    def test_discover_handle_failure_downgrades_gracefully(self) -> None:
        # If mDNS startup blows up (no network, multicast disabled),
        # the server must still build — the tools just degrade.
        with patch(
            "holo.discover._start_browser",
            side_effect=OSError("no multicast"),
        ):
            server = HoloMCPServer(no_bookmarklet=True)
            try:
                # discover_sessions returns empty rather than raising
                out = server.holo_discover_sessions()
                assert out == {"sessions": [], "count": 0}
                # fetch_capabilities raises with a clear error
                with pytest.raises(
                    RuntimeError, match="discover cache unavailable"
                ):
                    server.holo_fetch_capabilities("anything")
            finally:
                server.shutdown()


class TestCapabilityToolsAlwaysExposed:
    """The two tools must be registered regardless of bookmarklet/screen flags."""

    def _tool_names(self, **kwargs: Any) -> set[str]:
        import asyncio

        mcp, holo = build_server(**kwargs)
        try:
            tools = asyncio.run(mcp.list_tools())
            return {t.name for t in tools}
        finally:
            holo.shutdown()

    def test_no_bookmarklet_still_exposes_them(self) -> None:
        names = self._tool_names(no_bookmarklet=True)
        assert "holo_discover_sessions" in names
        assert "holo_fetch_capabilities" in names

    def test_default_build_exposes_them(self) -> None:
        names = self._tool_names(no_bookmarklet=False)
        assert "holo_discover_sessions" in names
        assert "holo_fetch_capabilities" in names
