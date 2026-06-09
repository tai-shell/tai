"""MCP server exposed by the tai control shell.

Registers the AppleScript-driven browser tools from
`holo.browser_chrome` as MCP tools, served over stdio. The screen
(`screen_*`) and template (`ui_template_*`) surfaces from the design
doc are deferred until the SikuliX bridge and pyobjc are bundled
into the binary — they'll be added here alongside browser_* when
they land.
"""

from __future__ import annotations

import atexit
import base64
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from holo import browser_chrome
from holo.bridge import BridgeClient


_app = FastMCP("tai-control-shell")

# ---------------------------------------------------------------------------
# SikuliX bridge — lazy. The JVM subprocess is only spawned on first
# screen_* / app_activate / ui_template_* call, so browser-only sessions
# never pay the ~1.5 s JVM cold-start cost or the ~250 MB resident set.
# ---------------------------------------------------------------------------

_bridge: BridgeClient | None = None


def _get_bridge() -> BridgeClient:
    global _bridge
    if _bridge is None:
        _bridge = BridgeClient()
        _bridge.start()
        atexit.register(_bridge.stop)
    return _bridge


# ---------------------------------------------------------------------------
# browser_* tools (AppleScript path)
#
# Each wrapper exists to give FastMCP a clean signature with explicit
# parameter names and types for tool-call schema generation. Bodies
# delegate straight to `holo.browser_chrome`.
# ---------------------------------------------------------------------------


@_app.tool()
def browser_navigate(url: str) -> dict[str, Any]:
    """Navigate the active Chrome tab to a URL."""
    return browser_chrome.navigate(url)


@_app.tool()
def browser_new_tab(url: str | None = None) -> dict[str, Any]:
    """Open a new Chrome tab, optionally navigated to URL."""
    return browser_chrome.new_tab(url)


@_app.tool()
def browser_close_active_tab() -> dict[str, Any]:
    """Close the active Chrome tab."""
    return browser_chrome.close_active_tab()


@_app.tool()
def browser_activate_tab(index: int) -> dict[str, Any]:
    """Activate the Chrome tab at the given 1-based index in the front window."""
    return browser_chrome.activate_tab(index)


@_app.tool()
def browser_list_tabs() -> dict[str, Any]:
    """List all Chrome tabs in the front window (index, title, url)."""
    return browser_chrome.list_tabs()


@_app.tool()
def browser_read_active_url() -> dict[str, Any]:
    """Return the URL of the active Chrome tab."""
    return browser_chrome.read_active_url()


@_app.tool()
def browser_read_active_title() -> dict[str, Any]:
    """Return the title of the active Chrome tab."""
    return browser_chrome.read_active_title()


@_app.tool()
def browser_reload() -> dict[str, Any]:
    """Reload the active Chrome tab."""
    return browser_chrome.reload()


@_app.tool()
def browser_back() -> dict[str, Any]:
    """Navigate the active Chrome tab back in history."""
    return browser_chrome.go_back()


@_app.tool()
def browser_forward() -> dict[str, Any]:
    """Navigate the active Chrome tab forward in history."""
    return browser_chrome.go_forward()


@_app.tool()
def browser_execute_js(js: str) -> dict[str, Any]:
    """Run a JS snippet in the active Chrome tab via Apple Events.

    Requires Chrome → View → Developer → "Allow JavaScript from Apple
    Events" to be enabled; otherwise raises JavaScriptNotAuthorized.
    """
    return browser_chrome.execute_js(js)


# ---------------------------------------------------------------------------
# screen_* tools (SikuliX bridge over JSON-RPC)
#
# First call spawns the JVM subprocess (~1.5 s cold). Subsequent calls
# reuse it. Java must be on PATH. The bridge locates its jar and
# Jython script via env vars set by embedded/boot.c.
# ---------------------------------------------------------------------------


@_app.tool()
def app_activate(name: str) -> dict[str, Any]:
    """Bring the named application to the foreground (e.g. "Google Chrome")."""
    return _get_bridge().activate(name)


@_app.tool()
def screen_click(
    x: int, y: int, modifiers: list[str] | None = None
) -> dict[str, Any]:
    """Click at screen coordinates, optionally holding modifiers (e.g. ["cmd"])."""
    return _get_bridge().click(x, y, modifiers=modifiers or [])


@_app.tool()
def screen_move(x: int, y: int) -> dict[str, Any]:
    """Move the cursor to (x, y). No click, no scroll, no key."""
    return _get_bridge().mouse_move(x, y)


@_app.tool()
def screen_type(text: str) -> dict[str, Any]:
    """Type a literal string into whatever has keyboard focus."""
    return _get_bridge().type_text(text)


@_app.tool()
def screen_key(combo: str) -> dict[str, Any]:
    """Send a key combo. Examples: "cmd+v", "enter", "shift+tab"."""
    return _get_bridge().key(combo)


@_app.tool()
def screen_scroll(
    x: int, y: int, direction: str = "down", steps: int = 3
) -> dict[str, Any]:
    """Move to (x, y) and emit `steps` mouse-wheel events in `direction`."""
    return _get_bridge().scroll(x, y, direction=direction, steps=steps)


@_app.tool()
def screen_shot(region: dict[str, int] | None = None) -> dict[str, Any]:
    """Capture the screen (or a region: {x, y, w, h}). Returns base64 PNG."""
    png = _get_bridge().screenshot(region=region)
    return {
        "image": base64.b64encode(png).decode("ascii"),
        "format": "png",
        "byte_count": len(png),
    }


@_app.tool()
def screen_find_image(
    needle: str,
    region: dict[str, int] | None = None,
    score: float = 0.7,
) -> dict[str, Any] | None:
    """Find `needle` (base64-encoded PNG) on screen. Returns coords or null."""
    try:
        needle_bytes = base64.b64decode(needle, validate=True)
    except Exception as e:
        raise ValueError("needle must be base64-encoded PNG") from e
    return _get_bridge().find_image(needle_bytes, region=region, score=score)


@_app.tool()
def screen_user_capture(
    prompt: str = "", timeout: float = 60.0
) -> dict[str, Any]:
    """Run the interactive drag-rectangle capture; return the dragged rect's PNG."""
    return _get_bridge().user_capture(prompt=prompt, timeout=timeout)


def serve() -> int:
    """Enter the MCP stdio serve loop. Returns when stdin EOFs."""
    tools = _app._tool_manager.list_tools()
    print(f"tai: control shell serving MCP over stdio "
          f"({len(tools)} tools: browser_*, screen_*, app_activate)",
          file=sys.stderr, flush=True)
    try:
        _app.run(transport="stdio")
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0
