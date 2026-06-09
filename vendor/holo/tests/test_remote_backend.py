"""Unit tests for `holo.remote_backend.RemoteHoloBackend`.

The MCP `ClientSession` and the `stdio_client` async context manager
are replaced with in-process fakes. Goals:

  - Verify the sync facade routes each method to the right tool name
    with the right argument shape (both input and capture surfaces).
  - Verify CallToolResult unwrapping handles `structuredContent`,
    falls back to text JSON, returns None for truly empty results
    (find_image depends on this to signal "no match"), and propagates
    `isError=true`.
  - Verify start() raises a clean error when the session fails to
    initialize, and stop() is idempotent.

No real subprocess is spawned and no real network I/O occurs.
"""

from __future__ import annotations

import base64 as _b64
import threading
from typing import Any

import pytest

from holo import remote_backend
from holo.remote_backend import RemoteHoloBackend, RemoteHoloError

# --- fakes ---------------------------------------------------------------


class _FakeCallToolResult:
    def __init__(
        self,
        *,
        structuredContent: dict[str, Any] | None = None,
        content: list[Any] | None = None,
        isError: bool = False,
    ) -> None:
        self.structuredContent = structuredContent
        self.content = content or []
        self.isError = isError


class _FakeTextContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeClientSession:
    """Records call_tool invocations; returns canned results."""

    _current: _FakeClientSession | None = None

    def __init__(self, read: Any, write: Any) -> None:
        self.read = read
        self.write = write
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.canned: dict[str, _FakeCallToolResult] = {}
        self.canned_exc: dict[str, BaseException] = {}
        self.init_called = False
        self.initialize_raises: BaseException | None = None
        _FakeClientSession._current = self

    async def __aenter__(self) -> _FakeClientSession:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def initialize(self) -> None:
        if self.initialize_raises is not None:
            raise self.initialize_raises
        self.init_called = True

    async def call_tool(self, name: str, params: dict[str, Any]) -> _FakeCallToolResult:
        self.calls.append((name, params))
        if name in self.canned_exc:
            raise self.canned_exc[name]
        return self.canned.get(name, _FakeCallToolResult(structuredContent={}))


class _FakeStdioClient:
    """Async context manager replacement for stdio_client."""

    last_params: Any = None

    def __init__(self, params: Any) -> None:
        _FakeStdioClient.last_params = params

    async def __aenter__(self) -> tuple[Any, Any]:
        return object(), object()

    async def __aexit__(self, *a: Any) -> bool:
        return False


@pytest.fixture(autouse=True)
def _patch_mcp_client(monkeypatch):
    monkeypatch.setattr(
        remote_backend, "stdio_client", lambda params: _FakeStdioClient(params)
    )
    monkeypatch.setattr(remote_backend, "ClientSession", _FakeClientSession)
    _FakeClientSession._current = None
    _FakeStdioClient.last_params = None
    yield


@pytest.fixture
def started_backend():
    backend = RemoteHoloBackend("test-host", 7777)
    backend.start()
    try:
        yield backend
    finally:
        backend.stop()


# --- input surface --------------------------------------------------------


def test_click_calls_screen_click_tool(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_click"] = _FakeCallToolResult(
        structuredContent={"clicked": True, "x": 100, "y": 200}
    )

    result = started_backend.click(100, 200)

    assert result == {"clicked": True, "x": 100, "y": 200}
    assert sess.calls == [("screen_click", {"x": 100, "y": 200, "modifiers": []})]


def test_click_with_modifiers(started_backend):
    started_backend.click(50, 60, modifiers=["cmd", "shift"])
    sess = _FakeClientSession._current
    assert sess.calls[-1] == (
        "screen_click", {"x": 50, "y": 60, "modifiers": ["cmd", "shift"]}
    )


def test_key_calls_screen_key(started_backend):
    started_backend.key("cmd+s")
    sess = _FakeClientSession._current
    assert sess.calls == [("screen_key", {"combo": "cmd+s"})]


def test_type_text_calls_screen_type(started_backend):
    started_backend.type_text("hello world")
    sess = _FakeClientSession._current
    assert sess.calls == [("screen_type", {"text": "hello world"})]


def test_scroll_calls_screen_scroll_with_defaults(started_backend):
    started_backend.scroll(300, 400)
    sess = _FakeClientSession._current
    assert sess.calls == [
        ("screen_scroll", {"x": 300, "y": 400, "direction": "down", "steps": 3})
    ]


def test_scroll_with_explicit_args(started_backend):
    started_backend.scroll(10, 20, direction="up", steps=5)
    sess = _FakeClientSession._current
    assert sess.calls[-1] == (
        "screen_scroll", {"x": 10, "y": 20, "direction": "up", "steps": 5}
    )


def test_mouse_move_calls_screen_move(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_move"] = _FakeCallToolResult(
        structuredContent={"moved": True, "x": 123, "y": 456}
    )

    result = started_backend.mouse_move(123, 456)

    assert result == {"moved": True, "x": 123, "y": 456}
    assert sess.calls == [("screen_move", {"x": 123, "y": 456})]


def test_activate_calls_app_activate(started_backend):
    started_backend.activate("Google Chrome")
    sess = _FakeClientSession._current
    assert sess.calls == [("app_activate", {"name": "Google Chrome"})]


# --- capture surface ------------------------------------------------------


def test_screenshot_returns_decoded_bytes(started_backend):
    sess = _FakeClientSession._current
    png_bytes = b"\x89PNG\r\n\x1a\nthe-actual-pixel-bytes"
    sess.canned["screen_shot"] = _FakeCallToolResult(
        structuredContent={
            "image": _b64.b64encode(png_bytes).decode("ascii"),
            "format": "png",
            "byte_count": len(png_bytes),
        }
    )

    result = started_backend.screenshot()

    assert result == png_bytes
    assert sess.calls == [("screen_shot", {"region": None})]


def test_screenshot_passes_region_through(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_shot"] = _FakeCallToolResult(
        structuredContent={
            "image": _b64.b64encode(b"x").decode("ascii"),
            "format": "png",
            "byte_count": 1,
        }
    )
    region = {"x": 10, "y": 20, "width": 100, "height": 200}

    started_backend.screenshot(region=region)

    assert sess.calls[-1] == ("screen_shot", {"region": region})


def test_screenshot_raises_on_unexpected_shape(started_backend):
    """If the remote returns something without an 'image' key, we don't
    silently corrupt — raise clearly so the agent sees the bug."""
    sess = _FakeClientSession._current
    sess.canned["screen_shot"] = _FakeCallToolResult(
        structuredContent={"oops": "no image here"}
    )

    with pytest.raises(RemoteHoloError, match="unexpected shape"):
        started_backend.screenshot()


def test_find_image_encodes_needle_and_returns_match(started_backend):
    sess = _FakeClientSession._current
    needle = b"\x89PNG\r\n\x1a\ntemplate"
    expected_b64 = _b64.b64encode(needle).decode("ascii")
    sess.canned["screen_find_image"] = _FakeCallToolResult(
        structuredContent={"x": 50, "y": 60, "width": 30, "height": 30, "score": 0.95}
    )

    result = started_backend.find_image(needle, score=0.85)

    assert result == {"x": 50, "y": 60, "width": 30, "height": 30, "score": 0.95}
    assert sess.calls == [
        ("screen_find_image", {"needle": expected_b64, "region": None, "score": 0.85})
    ]


def test_find_image_returns_none_when_no_match(started_backend):
    """When the remote tool returns None (no match), structuredContent is
    absent and content is empty — _call returns None and find_image
    surfaces None to the caller. BridgeClient.find_image has the same
    contract; agents check for None to retry / fall back."""
    sess = _FakeClientSession._current
    sess.canned["screen_find_image"] = _FakeCallToolResult()  # empty

    assert started_backend.find_image(b"needle") is None


def test_find_image_path_reads_file_locally(started_backend, tmp_path):
    sess = _FakeClientSession._current
    png_bytes = b"\x89PNG\r\n\x1a\nlocal-template"
    template = tmp_path / "kebab.png"
    template.write_bytes(png_bytes)
    sess.canned["screen_find_image"] = _FakeCallToolResult(
        structuredContent={"x": 1, "y": 2, "width": 3, "height": 4, "score": 0.9}
    )

    result = started_backend.find_image_path(template)

    assert result == {"x": 1, "y": 2, "width": 3, "height": 4, "score": 0.9}
    # The path is read locally and sent as base64 — the remote never sees
    # the local path string (its filesystem doesn't have our template cache).
    sent_needle = sess.calls[-1][1]["needle"]
    assert _b64.b64decode(sent_needle) == png_bytes


def test_find_image_path_raises_when_file_missing(started_backend, tmp_path):
    with pytest.raises(RemoteHoloError, match="could not read template"):
        started_backend.find_image_path(tmp_path / "does-not-exist.png")


def test_user_capture_proxies_to_screen_user_capture(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_user_capture"] = _FakeCallToolResult(
        structuredContent={
            "image": _b64.b64encode(b"selected-region-png").decode("ascii"),
            "x": 100, "y": 200, "width": 300, "height": 400,
        }
    )

    result = started_backend.user_capture(prompt="select the kebab", timeout=30.0)

    assert result["x"] == 100
    assert sess.calls == [
        ("screen_user_capture", {"prompt": "select the kebab", "timeout": 30.0})
    ]


def test_user_capture_cancelled(started_backend):
    """Esc → remote returns {cancelled: true, reason: ...}; we pass that
    through unchanged so the caller knows to re-prompt."""
    sess = _FakeClientSession._current
    sess.canned["screen_user_capture"] = _FakeCallToolResult(
        structuredContent={"cancelled": True, "reason": "user pressed esc"}
    )

    result = started_backend.user_capture()

    assert result == {"cancelled": True, "reason": "user pressed esc"}


# --- CallToolResult unwrapping -------------------------------------------


def test_structured_content_returned_verbatim(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_click"] = _FakeCallToolResult(
        structuredContent={"clicked": True, "x": 1, "y": 2}
    )
    assert started_backend.click(1, 2) == {"clicked": True, "x": 1, "y": 2}


def test_falls_back_to_parsing_text_json(started_backend):
    """When structuredContent is absent we parse the first text block as JSON."""
    sess = _FakeClientSession._current
    sess.canned["screen_key"] = _FakeCallToolResult(
        content=[_FakeTextContent('{"sent": "cmd+s"}')]
    )
    assert started_backend.key("cmd+s") == {"sent": "cmd+s"}


def test_non_json_text_wrapped(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_key"] = _FakeCallToolResult(
        content=[_FakeTextContent("plain text reply")]
    )
    assert started_backend.key("cmd+s") == {"text": "plain text reply"}


def test_empty_result_returns_none(started_backend):
    """Truly empty results (no structuredContent, no text) surface as
    None — find_image relies on this to signal "no match"."""
    sess = _FakeClientSession._current
    sess.canned["screen_key"] = _FakeCallToolResult()
    assert started_backend.key("cmd+s") is None


def test_is_error_raises_remote_holo_error(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_click"] = _FakeCallToolResult(
        isError=True,
        content=[_FakeTextContent("permission denied")],
    )
    with pytest.raises(RemoteHoloError, match="permission denied"):
        started_backend.click(1, 2)


def test_remote_call_exception_surfaces_as_remote_holo_error(started_backend):
    sess = _FakeClientSession._current
    sess.canned_exc["screen_type"] = ConnectionResetError("peer closed")
    with pytest.raises(RemoteHoloError, match="peer closed"):
        started_backend.type_text("x")


# --- lifecycle -----------------------------------------------------------


def test_start_passes_holo_connect_args_to_subprocess():
    backend = RemoteHoloBackend("10.0.0.5", 7081, holo_exe="/usr/local/bin/holo")
    backend.start()
    try:
        params = _FakeStdioClient.last_params
        assert params.command == "/usr/local/bin/holo"
        assert params.args == ["connect", "10.0.0.5:7081"]
    finally:
        backend.stop()


def test_start_raises_on_initialize_failure(monkeypatch):
    class _BoomSession(_FakeClientSession):
        async def initialize(self) -> None:
            raise ConnectionRefusedError("nothing on port 7777")

    monkeypatch.setattr(remote_backend, "ClientSession", _BoomSession)

    backend = RemoteHoloBackend("h", 7777)
    with pytest.raises(RemoteHoloError, match="nothing on port 7777"):
        backend.start()


def test_stop_is_idempotent(started_backend):
    started_backend.stop()
    started_backend.stop()


def test_call_after_stop_raises(started_backend):
    started_backend.stop()
    with pytest.raises(RemoteHoloError, match="already stopped"):
        started_backend.click(1, 2)


def test_start_is_idempotent(started_backend):
    first_session = _FakeClientSession._current
    started_backend.start()  # no-op
    assert _FakeClientSession._current is first_session


# --- backward-compat shim -------------------------------------------------


def test_remote_input_shim_aliases_still_work():
    """`from holo.remote_input import RemoteInputBackend, RemoteInputError`
    must keep working for any code that hasn't been updated to the new
    module name. The aliases resolve to the renamed classes."""
    from holo.remote_input import RemoteInputBackend, RemoteInputError

    assert RemoteInputBackend is RemoteHoloBackend
    assert RemoteInputError is RemoteHoloError


# --- multi-threaded use --------------------------------------------------


def test_concurrent_calls_from_threads_dont_deadlock(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_click"] = _FakeCallToolResult(
        structuredContent={"clicked": True}
    )

    results: list[Any] = []
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            results.append(started_backend.click(i, i))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert errors == [], f"unexpected errors: {errors}"
    assert len(results) == 8
    assert len(sess.calls) == 8
