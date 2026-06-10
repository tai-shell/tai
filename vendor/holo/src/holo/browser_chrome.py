"""AppleScript-driven Chrome browser ops.

The SikuliX bridge can drive Chrome via synthetic keystrokes
(`screen_key "cmd+l"` → `screen_type` → `screen_key "enter"`), but
that approach is timing-sensitive on macOS Sonoma+: `app_activate`
returns before the OS has actually shifted focus, keystrokes land
in the wrong window, beep, get dropped. Chrome's AppleScript
dictionary sidesteps the whole keyboard layer — `set URL of active
tab of front window to "..."` is synchronous and reliable, no
focus race, no beeps.

This module wraps the relevant subset of Chrome's AppleScript
surface as plain Python calls. `osascript` is a child process with
the daemon as parent; macOS gates this with Automation permission
for whatever app launched the daemon (Terminal, the tmux server,
etc.) — one TCC prompt the first time `browser_*` is used.

macOS-only. Linux/Windows browser ops will land in Phase 3 via
CDP. The bookmarklet channel is unchanged — it's still the right
tool for in-page DOM reads where AppleScript is too coarse.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any

OSASCRIPT_TIMEOUT_S = 10.0
APP_NAME = "Google Chrome"

# AppleScript delimiters chosen to not appear in URLs or page titles.
# Unit Separator \x1f between fields of a tab; Record Separator \x1e
# between tabs.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


class BrowserError(RuntimeError):
    """Raised when osascript exits non-zero or returns unparseable output."""


class BrowserNotAvailable(RuntimeError):
    """Raised when the platform can't drive Chrome via AppleScript
    (non-macOS, or osascript missing)."""


class JavaScriptNotAuthorized(BrowserError):
    """Raised when Chrome's "Allow JavaScript from Apple Events" toggle
    is OFF and `browser_execute_js` is called. Distinct from generic
    BrowserError so callers can route to a bookmarklet fallback when
    a calibrated channel is available."""


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise BrowserNotAvailable(
            f"Chrome AppleScript adapter is macOS-only; running on "
            f"{sys.platform}. Phase 3 CDP adapter will cover other platforms."
        )
    if shutil.which("osascript") is None:
        raise BrowserNotAvailable("osascript not found on PATH")


def _run_applescript(script: str, *, timeout: float = OSASCRIPT_TIMEOUT_S) -> str:
    """Run an AppleScript snippet, return its stdout (text), raise
    BrowserError on non-zero exit."""
    _require_macos()
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise BrowserError(
            f"osascript timed out after {timeout}s"
        ) from e
    if proc.returncode != 0:
        # osascript writes errors to stderr, prefixed with "execution
        # error:" or similar. Surface verbatim, but distinguish the
        # specific "JS from Apple Events not authorized" case so
        # callers can fall back to the bookmarklet channel.
        msg = proc.stderr.strip() or proc.stdout.strip() or "(no output)"
        if _is_js_not_authorized(msg):
            raise JavaScriptNotAuthorized(
                "Chrome's 'Allow JavaScript from Apple Events' is off. "
                "Enable it in Chrome → View → Developer → "
                "'Allow JavaScript from Apple Events', then retry. "
                "Original error: " + msg
            )
        raise BrowserError(f"osascript exit {proc.returncode}: {msg}")
    return proc.stdout.rstrip("\n")


def _is_js_not_authorized(stderr: str) -> bool:
    """Detect the specific Chrome error when the AppleScript-execute-JS
    toggle is off. Chrome's exact wording across versions:

      - "Executing JavaScript through AppleScript is turned off."
      - "Allow JavaScript from Apple Events"
      - errAEEventNotPermitted (-1743)
    """
    s = stderr.lower()
    return (
        "executing javascript through applescript is turned off" in s
        or "allow javascript from apple events" in s
        or "javascript through apple events" in s
        or "-1743" in s  # errAEEventNotPermitted
    )


# ---- script builders -----------------------------------------------


def _navigate_script(url: str) -> str:
    return (
        f'tell application "{APP_NAME}"\n'
        f'  set URL of active tab of front window to "{_escape(url)}"\n'
        "end tell"
    )


def _new_tab_script(url: str | None) -> str:
    if url is None:
        props = ""
    else:
        props = f' with properties {{URL:"{_escape(url)}"}}'
    return (
        f'tell application "{APP_NAME}"\n'
        "  tell front window\n"
        f"    make new tab at end of tabs{props}\n"
        "  end tell\n"
        "end tell"
    )


def _close_active_tab_script() -> str:
    return (
        f'tell application "{APP_NAME}"\n'
        "  close active tab of front window\n"
        "end tell"
    )


def _activate_tab_script(index: int) -> str:
    return (
        f'tell application "{APP_NAME}"\n'
        f"  set active tab index of front window to {index}\n"
        "  activate\n"
        "end tell"
    )


def _read_field_script(field: str) -> str:
    return (
        f'tell application "{APP_NAME}"\n'
        f"  return {field} of active tab of front window\n"
        "end tell"
    )


def _reload_script() -> str:
    return (
        f'tell application "{APP_NAME}"\n'
        "  reload active tab of front window\n"
        "end tell"
    )


def _history_script(direction: str) -> str:
    """direction is 'go back' or 'go forward'."""
    return (
        f'tell application "{APP_NAME}"\n'
        f"  tell active tab of front window to {direction}\n"
        "end tell"
    )


def _execute_js_script(js: str) -> str:
    """Wrap a JS expression for Chrome's `execute javascript` AppleScript
    command. The expression must be a single value-producing JS
    expression (or an IIFE that returns one); Chrome serializes
    primitives as their text form. For structured data, wrap in
    `JSON.stringify(...)` on the caller's side."""
    return (
        f'tell application "{APP_NAME}"\n'
        f'  return execute active tab of front window javascript "{_escape(js)}"\n'
        "end tell"
    )


def _list_tabs_script() -> str:
    """Return all tabs of the front window as
    `id<US>title<US>url<US>index<RS>...`. AppleScript's `character id N`
    inserts a literal Unicode codepoint."""
    return (
        f'tell application "{APP_NAME}"\n'
        '  set out to ""\n'
        "  set i to 1\n"
        "  set winTabs to tabs of front window\n"
        "  repeat with t in winTabs\n"
        "    set out to out & (id of t as text) & (character id 31)"
        " & (title of t) & (character id 31)"
        " & (URL of t) & (character id 31)"
        " & (i as text) & (character id 30)\n"
        "    set i to i + 1\n"
        "  end repeat\n"
        "  set activeIdx to active tab index of front window\n"
        '  return out & "ACTIVE=" & (activeIdx as text)\n'
        "end tell"
    )


def _escape(s: str) -> str:
    """Escape a string for safe interpolation into an AppleScript
    double-quoted literal. AppleScript's escape rules are simpler than
    JSON's: backslash and double-quote need backslash-escaping."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---- public API ----------------------------------------------------


def navigate(url: str) -> dict[str, Any]:
    """Set the active tab's URL. Returns the new URL."""
    if not url:
        raise BrowserError("url must be non-empty")
    _run_applescript(_navigate_script(url))
    return {"url": url}


def new_tab(url: str | None = None) -> dict[str, Any]:
    """Open a new tab in the front window. Returns the new tab's URL."""
    _run_applescript(_new_tab_script(url))
    return {"url": url or "chrome://newtab/"}


def close_active_tab() -> dict[str, Any]:
    """Close the active tab of the front window."""
    _run_applescript(_close_active_tab_script())
    return {"closed": True}


def activate_tab(index: int) -> dict[str, Any]:
    """Make tab `index` (1-based) the active tab of the front window
    and bring Chrome to the foreground."""
    if index < 1:
        raise BrowserError("tab index is 1-based; use 1 for the first tab")
    _run_applescript(_activate_tab_script(index))
    return {"index": index}


def read_active_url() -> dict[str, Any]:
    return {"url": _run_applescript(_read_field_script("URL"))}


def read_active_title() -> dict[str, Any]:
    return {"title": _run_applescript(_read_field_script("title"))}


def reload() -> dict[str, Any]:
    _run_applescript(_reload_script())
    return {"reloaded": True}


def go_back() -> dict[str, Any]:
    _run_applescript(_history_script("go back"))
    return {"direction": "back"}


def go_forward() -> dict[str, Any]:
    _run_applescript(_history_script("go forward"))
    return {"direction": "forward"}


def execute_js(js: str) -> dict[str, Any]:
    """Run a JS expression in Chrome's active tab via AppleScript and
    return its result. Requires Chrome's "Allow JavaScript from Apple
    Events" toggle (View → Developer); raises `JavaScriptNotAuthorized`
    if it's off so callers can fall back to the bookmarklet channel.

    The result is whatever Chrome's stringification of the JS value
    produces — primitives come back as their text form, objects as
    `[object Object]` (so callers should `JSON.stringify` themselves
    if they want structured data).
    """
    if not js:
        raise BrowserError("js expression must be non-empty")
    raw = _run_applescript(_execute_js_script(js))
    return {"result": raw}


def list_tabs() -> dict[str, Any]:
    """Snapshot of the front window's tabs.

    Returns `{tabs: [{id, title, url, index}, ...], active: index}`.
    """
    raw = _run_applescript(_list_tabs_script())
    return _parse_list_tabs(raw)


def _parse_list_tabs(raw: str) -> dict[str, Any]:
    """Parse the delimiter-encoded tab list from `_list_tabs_script`."""
    if "ACTIVE=" not in raw:
        raise BrowserError(f"unexpected list_tabs output: {raw!r}")
    body, _, active_part = raw.rpartition("ACTIVE=")
    try:
        active = int(active_part.strip())
    except ValueError as e:
        raise BrowserError(f"bad active index in list_tabs: {active_part!r}") from e

    tabs = []
    # Each record ends with RS; final record may or may not have a trailing RS
    # depending on AppleScript's stringification. Split and drop empty tail.
    for record in body.split(_RECORD_SEP):
        if not record:
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) != 4:
            raise BrowserError(
                f"bad tab record (expected 4 fields, got {len(fields)}): "
                f"{record!r}"
            )
        tab_id, title, url, index_s = fields
        try:
            index = int(index_s)
        except ValueError as e:
            raise BrowserError(
                f"bad tab index {index_s!r} in record {record!r}"
            ) from e
        try:
            tab_id_v: int | str = int(tab_id)
        except ValueError:
            tab_id_v = tab_id
        tabs.append({"id": tab_id_v, "title": title, "url": url, "index": index})

    return {"tabs": tabs, "active": active}
