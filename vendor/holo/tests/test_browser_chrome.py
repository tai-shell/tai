"""Tests for `holo.browser_chrome` — AppleScript-driven Chrome ops.

We never invoke `osascript` here; `subprocess.run` is patched.
The goal is to lock the AppleScript snippets we generate (so a
typo doesn't ship silently) and the parsing of `list_tabs`'s
delimiter-encoded output.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from holo import browser_chrome
from holo.browser_chrome import BrowserError, BrowserNotAvailable, JavaScriptNotAuthorized

# All these tests pretend we're on macOS regardless of where the suite runs.
pytestmark = [
    pytest.mark.usefixtures("force_darwin"),
]


@pytest.fixture
def force_darwin(monkeypatch):
    monkeypatch.setattr(browser_chrome.sys, "platform", "darwin")
    monkeypatch.setattr(browser_chrome.shutil, "which", lambda _: "/usr/bin/osascript")


def _ok(stdout: str = "") -> MagicMock:
    p = MagicMock()
    p.returncode = 0
    p.stdout = stdout
    p.stderr = ""
    return p


def _fail(stderr: str, code: int = 1) -> MagicMock:
    p = MagicMock()
    p.returncode = code
    p.stdout = ""
    p.stderr = stderr
    return p


# --- script content --------------------------------------------------


def test_navigate_invokes_osascript_with_chrome_applescript():
    with patch.object(subprocess, "run", return_value=_ok()) as run:
        result = browser_chrome.navigate("https://example.com")

    assert result == {"url": "https://example.com"}
    assert run.call_count == 1
    cmd = run.call_args.args[0]
    assert cmd[0] == "osascript"
    assert cmd[1] == "-e"
    script = cmd[2]
    assert 'tell application "Google Chrome"' in script
    assert "set URL of active tab of front window" in script
    assert '"https://example.com"' in script


def test_navigate_escapes_quotes_and_backslashes():
    """A URL with a `"` shouldn't break out of the AppleScript literal."""
    nasty = 'https://example.com/?q="; do shell script "rm -rf /'
    with patch.object(subprocess, "run", return_value=_ok()) as run:
        browser_chrome.navigate(nasty)

    script = run.call_args.args[0][2]
    # The escaped form should appear in the script; the unescaped form should not.
    assert '\\"' in script
    assert 'do shell script "rm' not in script.replace("\\\"", "")


def test_navigate_rejects_empty_url():
    with pytest.raises(BrowserError, match="non-empty"):
        browser_chrome.navigate("")


def test_new_tab_with_url():
    with patch.object(subprocess, "run", return_value=_ok()) as run:
        result = browser_chrome.new_tab("https://example.com")
    assert result == {"url": "https://example.com"}
    script = run.call_args.args[0][2]
    assert "make new tab" in script
    assert '{URL:"https://example.com"}' in script


def test_new_tab_without_url_omits_properties():
    with patch.object(subprocess, "run", return_value=_ok()) as run:
        result = browser_chrome.new_tab()
    assert result == {"url": "chrome://newtab/"}
    script = run.call_args.args[0][2]
    assert "make new tab at end of tabs\n" in script
    assert "with properties" not in script


def test_close_active_tab():
    with patch.object(subprocess, "run", return_value=_ok()) as run:
        browser_chrome.close_active_tab()
    assert "close active tab of front window" in run.call_args.args[0][2]


def test_activate_tab():
    with patch.object(subprocess, "run", return_value=_ok()) as run:
        browser_chrome.activate_tab(3)
    script = run.call_args.args[0][2]
    assert "set active tab index of front window to 3" in script
    assert "activate" in script


def test_activate_tab_rejects_zero_or_negative():
    with pytest.raises(BrowserError, match="1-based"):
        browser_chrome.activate_tab(0)


def test_read_active_url():
    with patch.object(subprocess, "run", return_value=_ok("https://x.test/\n")):
        result = browser_chrome.read_active_url()
    assert result == {"url": "https://x.test/"}


def test_read_active_title():
    with patch.object(subprocess, "run", return_value=_ok("My Page Title\n")):
        result = browser_chrome.read_active_title()
    assert result == {"title": "My Page Title"}


def test_reload():
    with patch.object(subprocess, "run", return_value=_ok()) as run:
        browser_chrome.reload()
    assert "reload active tab" in run.call_args.args[0][2]


def test_back_and_forward():
    with patch.object(subprocess, "run", return_value=_ok()) as run:
        browser_chrome.go_back()
    assert "go back" in run.call_args.args[0][2]

    with patch.object(subprocess, "run", return_value=_ok()) as run:
        browser_chrome.go_forward()
    assert "go forward" in run.call_args.args[0][2]


# --- list_tabs parsing ----------------------------------------------


def _encode_tabs(tabs: list[tuple[int, str, str, int]], active: int) -> str:
    """Build the same delimiter-encoded payload AppleScript emits."""
    parts = []
    for tid, title, url, idx in tabs:
        parts.append(
            f"{tid}\x1f{title}\x1f{url}\x1f{idx}\x1e"
        )
    return "".join(parts) + f"ACTIVE={active}"


def test_list_tabs_parses_single_tab():
    raw = _encode_tabs([(101, "Example", "https://example.com/", 1)], active=1)
    with patch.object(subprocess, "run", return_value=_ok(raw)):
        result = browser_chrome.list_tabs()
    assert result == {
        "tabs": [
            {"id": 101, "title": "Example", "url": "https://example.com/", "index": 1}
        ],
        "active": 1,
    }


def test_list_tabs_parses_multiple_tabs():
    raw = _encode_tabs(
        [
            (1, "first",  "https://a/", 1),
            (2, "second", "https://b/", 2),
            (3, "third",  "https://c/", 3),
        ],
        active=2,
    )
    with patch.object(subprocess, "run", return_value=_ok(raw)):
        result = browser_chrome.list_tabs()
    assert [t["title"] for t in result["tabs"]] == ["first", "second", "third"]
    assert result["active"] == 2


def test_list_tabs_handles_titles_with_pipes_and_spaces():
    """Titles can contain anything; our delimiters \\x1f \\x1e shouldn't
    collide with realistic content."""
    raw = _encode_tabs(
        [(7, "Foo | Bar - my page", "https://x/?a=1&b=2", 1)],
        active=1,
    )
    with patch.object(subprocess, "run", return_value=_ok(raw)):
        result = browser_chrome.list_tabs()
    assert result["tabs"][0]["title"] == "Foo | Bar - my page"
    assert result["tabs"][0]["url"] == "https://x/?a=1&b=2"


def test_list_tabs_falls_back_to_string_id_if_not_int():
    """Chrome IDs are integers in practice, but the parser shouldn't crash
    if AppleScript ever returns non-numeric ones (some Chromium forks)."""
    raw = "abc-123\x1ftitle\x1fhttps://x/\x1f1\x1e" + "ACTIVE=1"
    with patch.object(subprocess, "run", return_value=_ok(raw)):
        result = browser_chrome.list_tabs()
    assert result["tabs"][0]["id"] == "abc-123"


def test_list_tabs_rejects_malformed_active():
    raw = "1\x1ftitle\x1fhttps://x/\x1f1\x1e" + "ACTIVE=oops"
    with patch.object(subprocess, "run", return_value=_ok(raw)):
        with pytest.raises(BrowserError, match="bad active index"):
            browser_chrome.list_tabs()


def test_list_tabs_rejects_short_record():
    raw = "1\x1ftitle\x1fhttps://x/\x1e" + "ACTIVE=1"  # 3 fields not 4
    with patch.object(subprocess, "run", return_value=_ok(raw)):
        with pytest.raises(BrowserError, match="bad tab record"):
            browser_chrome.list_tabs()


def test_list_tabs_rejects_missing_active_marker():
    with patch.object(subprocess, "run", return_value=_ok("garbage")):
        with pytest.raises(BrowserError, match="unexpected list_tabs output"):
            browser_chrome.list_tabs()


# --- error handling -------------------------------------------------


def test_osascript_failure_surfaces_stderr():
    """When osascript exits non-zero, the stderr text reaches our caller."""
    err = "execution error: Application isn't running. (-600)"
    with patch.object(subprocess, "run", return_value=_fail(err)):
        with pytest.raises(BrowserError, match="Application isn't running"):
            browser_chrome.navigate("https://example.com")


def test_osascript_timeout():
    with patch.object(
        subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd=["osascript"], timeout=10.0),
    ):
        with pytest.raises(BrowserError, match="timed out"):
            browser_chrome.navigate("https://example.com")


# --- platform gating ------------------------------------------------


def test_non_macos_raises_browser_not_available(monkeypatch):
    monkeypatch.setattr(browser_chrome.sys, "platform", "linux")
    with pytest.raises(BrowserNotAvailable, match="macOS-only"):
        browser_chrome.navigate("https://example.com")


def test_missing_osascript_raises_browser_not_available(monkeypatch):
    monkeypatch.setattr(browser_chrome.shutil, "which", lambda _: None)
    with pytest.raises(BrowserNotAvailable, match="osascript not found"):
        browser_chrome.navigate("https://example.com")


# --- Sanity: real-platform behaviour on Linux/Windows works correctly --


def test_execute_js_returns_stringified_result():
    with patch.object(subprocess, "run", return_value=_ok("hello\n")) as run:
        result = browser_chrome.execute_js("document.title")
    assert result == {"result": "hello"}
    script = run.call_args.args[0][2]
    assert "execute active tab of front window javascript" in script
    assert '"document.title"' in script


def test_execute_js_escapes_quotes_in_expression():
    nasty = 'document.querySelector("a.cta")?.innerText'
    with patch.object(subprocess, "run", return_value=_ok("Click me")) as run:
        browser_chrome.execute_js(nasty)
    script = run.call_args.args[0][2]
    # Inner double-quotes must be backslash-escaped or AppleScript breaks.
    assert '\\"a.cta\\"' in script


def test_execute_js_rejects_empty_expression():
    with pytest.raises(BrowserError, match="non-empty"):
        browser_chrome.execute_js("")


def test_execute_js_raises_javascript_not_authorized_when_toggle_off():
    """Chrome's exact stderr when 'Allow JavaScript from Apple Events' is off
    varies across versions; cover the documented patterns."""
    err_messages = [
        "Executing JavaScript through AppleScript is turned off.",
        "execution error: AppleScript permission required (-1743)",
        "javascript through apple events is not allowed",
    ]
    for err in err_messages:
        with patch.object(subprocess, "run", return_value=_fail(err)):
            with pytest.raises(JavaScriptNotAuthorized) as exc:
                browser_chrome.execute_js("document.title")
        assert "View → Developer" in str(exc.value)
        assert "Allow JavaScript from Apple Events" in str(exc.value)


def test_execute_js_other_errors_remain_browser_error():
    """Unrelated osascript errors don't get the JS-not-authorized
    treatment — important so callers don't get confused about whether
    a fallback is appropriate."""
    with patch.object(
        subprocess, "run", return_value=_fail("execution error: -1728 (-1728)")
    ):
        with pytest.raises(BrowserError) as exc:
            browser_chrome.execute_js("document.title")
    assert not isinstance(exc.value, JavaScriptNotAuthorized)


def test_running_on_actual_non_macos_without_force_skips_gracefully():
    """If you happen to run this suite on Linux, the platform check fires
    cleanly. This isn't asserting anything new; it's a guard rail so the
    test file doesn't crash on a Linux CI runner."""
    if sys.platform == "darwin":
        pytest.skip("only meaningful off-macOS")
    with pytest.raises(BrowserNotAvailable):
        browser_chrome.navigate("https://example.com")
