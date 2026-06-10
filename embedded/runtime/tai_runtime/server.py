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
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP

from holo import browser_chrome
from holo.bridge import BridgeClient
from holo.templates import TemplateNotFound, TemplateStore


_app = FastMCP("tai-control-shell")

# ---------------------------------------------------------------------------
# SikuliX bridge — lazy. The JVM subprocess is only spawned on first
# screen_* / app_activate / ui_template_* call, so browser-only sessions
# never pay the ~1.5 s JVM cold-start cost or the ~250 MB resident set.
#
# The double-checked-locking + local-binding pattern below handles two
# hazards together:
#   1. FastMCP can dispatch tools concurrently from its worker pool —
#      two callers racing on `_bridge is None` without a lock would
#      both spawn a JVM and leak the loser.
#   2. The previous shape assigned `_bridge = BridgeClient()` BEFORE
#      `.start()`, so a start() failure left a poisoned non-None
#      singleton that later calls skipped past. Construct + start
#      into a local first; only publish on success.
# ---------------------------------------------------------------------------

_bridge: BridgeClient | None = None
_templates: TemplateStore | None = None
_bridge_lock = threading.Lock()
_templates_lock = threading.Lock()


def _get_bridge() -> BridgeClient:
    global _bridge
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                tmp = BridgeClient()
                tmp.start()
                atexit.register(tmp.stop)
                _bridge = tmp
    return _bridge


def _get_templates() -> TemplateStore:
    """Lazy template store. Root path comes from HOLO_TEMPLATE_DIR
    which embedded/boot.c sets to ~/Library/Caches/tai/templates."""
    global _templates
    if _templates is None:
        with _templates_lock:
            if _templates is None:
                _templates = TemplateStore()
    return _templates


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


# ---------------------------------------------------------------------------
# ui_template_* tools — persistent name → PNG cache for stable UI elements.
#
# Bodies are full-fidelity ports of HoloMCPServer.ui_template_* (vendor/holo/
# src/holo/mcp_server.py lines 493-610) with `self.templates` and
# `self._capture_target() / _input_target()` rewritten to call our lazy
# accessors. Tool docstrings and parameter defaults match holo verbatim so
# agents trained against standalone holo work against tai with no prompt
# changes.
# ---------------------------------------------------------------------------


@_app.tool(
    description=(
        "Save a template image for `(app, label)`. If `region` is "
        "provided, captures that screen rect; otherwise blocks for the "
        "user to drag-select a rectangle (Esc cancels). `app` defaults "
        "to '_global'. Pass `replace=True` to discard existing variants; "
        "otherwise the new image is added as another variant (idle, "
        "hover, dark mode, etc.). Returns the saved index entry, or "
        "{cancelled: true} if the user pressed Esc."
    )
)
def ui_template_capture(
    label: str,
    app: str | None = None,
    region: dict[str, int] | None = None,
    replace: bool = False,
    similarity: float = 0.85,
    timeout: float = 60.0,
    prompt: str = "",
) -> dict[str, Any]:
    capture = _get_bridge()
    if region is not None:
        png = capture.screenshot(region=region)
    else:
        result = capture.user_capture(prompt=prompt, timeout=timeout)
        if result.get("cancelled"):
            return {
                "cancelled": True,
                "reason": result.get("reason", "user cancelled"),
            }
        png = base64.b64decode(result["image"])
    entry = _get_templates().add_variant(
        label, app, png, replace=replace, similarity=similarity
    )
    return {"saved": True, "entry": entry}


@_app.tool(
    description=(
        "List stored UI templates. `app=None` lists everything; pass an "
        "app name (or '_global') to filter."
    )
)
def ui_template_list(app: str | None = None) -> dict[str, Any]:
    return {"templates": _get_templates().list(app=app)}


@_app.tool(
    description=(
        "Locate a saved template on the current screen. Walks variants "
        "in order, returns the first hit as {x, y, width, height, score, "
        "variant} or null if none match. Raises if the label has no "
        "registered template — call `ui_template_capture` first."
    )
)
def ui_template_find(
    label: str,
    app: str | None = None,
    region: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    store = _get_templates()
    try:
        paths = store.variant_paths(label, app)
    except TemplateNotFound as e:
        raise LookupError(str(e)) from e
    entry = store.get(label, app)
    # `get` is checked because `variant_paths` would have raised already
    # if the entry was missing; this is just an annotation for type
    # narrowing.
    score = float(entry["similarity"]) if entry else 0.85
    capture = _get_bridge()
    for p in paths:
        match = capture.find_image_path(str(p), region=region, score=score)
        if match is not None:
            store.touch(label, app)
            return {**match, "variant": p.name}
    return None


@_app.tool(
    description=(
        "Find a saved template and click its center. Left-single-click "
        "only — the underlying SikuliX bridge does not expose button or "
        "click-count yet, so we omit them from the schema rather than "
        "advertise params we silently discard. Raises if nothing matches."
    )
)
def ui_template_click(
    label: str,
    app: str | None = None,
    region: dict[str, int] | None = None,
) -> dict[str, Any]:
    match = ui_template_find(label, app, region=region)
    if match is None:
        app_norm = app or "_global"
        raise RuntimeError(
            f"template {app_norm}/{label} matched nothing on screen "
            "(use ui_template_capture to refresh, or check whether the "
            "target app is in front)"
        )
    cx = int(match["x"] + match["width"] / 2)
    cy = int(match["y"] + match["height"] / 2)
    _get_bridge().click(cx, cy)
    return {
        "clicked": True,
        "x": cx,
        "y": cy,
        "score": match["score"],
        "variant": match["variant"],
    }


@_app.tool(
    description=(
        "Remove a stored template entry, or pass `variant` to delete just "
        "one variant (the entry stays if other variants remain)."
    )
)
def ui_template_delete(
    label: str,
    app: str | None = None,
    variant: str | None = None,
) -> dict[str, Any]:
    removed = _get_templates().delete(label, app, variant=variant)
    return {"removed": removed}


def serve() -> int:
    """Enter the MCP stdio serve loop. Returns when stdin EOFs."""
    tools = _app._tool_manager.list_tools()
    print(f"tai: control shell serving MCP over stdio "
          f"({len(tools)} tools: browser_*, screen_*, ui_template_*, "
          "app_activate)", file=sys.stderr, flush=True)
    try:
        _app.run(transport="stdio")
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0
