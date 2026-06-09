"""MCP server exposed by the tai control shell.

Registers the AppleScript-driven browser tools from
`holo.browser_chrome` as MCP tools, served over stdio. The screen
(`screen_*`) and template (`ui_template_*`) surfaces from the design
doc are deferred until the SikuliX bridge and pyobjc are bundled
into the binary — they'll be added here alongside browser_* when
they land.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from holo import browser_chrome


_app = FastMCP("tai-control-shell")


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


def serve() -> int:
    """Enter the MCP stdio serve loop. Returns when stdin EOFs."""
    print("tai: control shell serving MCP over stdio "
          f"({len(_app._tool_manager.list_tools())} tools registered)",
          file=sys.stderr, flush=True)
    try:
        _app.run(transport="stdio")
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0
