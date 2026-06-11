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
import os
import sys
import threading
from typing import Any


# ---------------------------------------------------------------------------
# Workaround for CPython embedded-interpreter env-var visibility.
#
# embedded/boot.c does `setenv("HOLO_SIKULI_JAR", ...)` BEFORE
# Py_InitializeFromConfig so vendored holo's `os.environ.get(...)`
# lookups would see them. But CPython's posix module imports its
# `environ` snapshot at a point that, when we're embedded inside
# bash, doesn't reflect our pre-Py-init setenv calls — leaving
# HOLO_SIKULI_JAR / HOLO_BRIDGE_SCRIPT / HOLO_TEMPLATE_DIR invisible
# to anything reading them via os.environ.
#
# The values are in libc's environ (verified via ctypes getenv), so
# we backfill os.environ at import time before any holo module
# does its env-var reads. This has to happen BEFORE the
# `from holo.bridge import BridgeClient` line below or it's too
# late — BridgeClient reads the env at class-definition time.
# ---------------------------------------------------------------------------


def _backfill_env_from_libc() -> None:
    import ctypes
    try:
        libc = ctypes.CDLL(None)
        libc.getenv.restype = ctypes.c_char_p
        libc.getenv.argtypes = [ctypes.c_char_p]
    except OSError:
        return
    for name in (
        "HOLO_SIKULI_JAR",
        "HOLO_BRIDGE_SCRIPT",
        "HOLO_TEMPLATE_DIR",
    ):
        if name in os.environ:
            continue
        raw = libc.getenv(name.encode())
        if raw is not None:
            os.environ[name] = raw.decode()


_backfill_env_from_libc()


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
# doctor — runtime/permissions/environment diagnostic
# ---------------------------------------------------------------------------


@_app.tool(
    description=(
        "Diagnose the tai/tcsh runtime: Java, SikuliX bridge, "
        "Screen Recording (via window-title readability), the TCC "
        "responsible-process chain, and bundle-vs-dev-tree state. "
        "Does NOT start the JVM bridge (cheap to run). Returns a dict; "
        "use `holo doctor | jq` to read it from the shell."
    )
)
def doctor() -> dict[str, Any]:
    return _run_doctor()


def _run_doctor() -> dict[str, Any]:
    import os
    import platform
    import shutil
    import subprocess
    from importlib.metadata import version as _v

    result: dict[str, Any] = {}

    # Runtime / platform
    result["platform"] = {
        "system": platform.system(),
        "machine": platform.machine(),
        "release": platform.mac_ver()[0] if sys.platform == "darwin" else None,
    }
    result["python"] = {
        "version": sys.version.split()[0],
        "executable": sys.executable,
        "prefix": sys.prefix,
    }
    result["process"] = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "argv0": sys.argv[0] if sys.argv else None,
    }

    # The three HOLO_* env vars boot.c set up — confirm they point
    # at extant files. The libc-backfill at import time above makes
    # these visible via os.environ.
    result["env"] = {
        name: _check_path(os.environ.get(name))
        for name in ("HOLO_SIKULI_JAR", "HOLO_BRIDGE_SCRIPT", "HOLO_TEMPLATE_DIR")
    }

    # Java — top failure mode on a fresh macOS install.
    result["java"] = _check_java()

    # TCC chain: walk the parent pid up. macOS attributes
    # Accessibility / Screen Recording permissions to the
    # responsible process up the launch chain — usually an .app
    # ancestor (Terminal.app, iTerm.app, etc.), NOT the binary
    # itself. Adding tai to Accessibility doesn't help if the
    # ancestor is what TCC is checking against.
    result["tcc_chain"] = _walk_tcc_chain()

    # Screen Recording probe: count visible windows and how many
    # have readable titles. Without Screen Recording, browser
    # window titles read as empty. Mirrors holo's check.
    result["screen_recording"] = _check_window_listing()

    # Bundled-payload state: which mode, which cache.
    result["payload"] = _check_payload_state()

    # MCP framework version, holo subtree version.
    result["versions"] = {
        "holo": _safe(lambda: __import__("holo").__version__),
        "mcp": _safe(lambda: _v("mcp")),
    }

    # Verdict — short human-readable summary
    result["summary"] = _summarize(result)
    return result


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        return f"<error: {e}>"


def _check_path(p: str | None) -> dict[str, Any]:
    if p is None:
        return {"set": False}
    import os
    return {
        "set": True,
        "value": p,
        "exists": os.path.exists(p),
        "size_bytes": (os.path.getsize(p) if os.path.isfile(p) else None),
    }


def _check_java() -> dict[str, Any]:
    import shutil
    import subprocess

    java = shutil.which("java")
    if not java:
        return {
            "available": False,
            "hint": (
                "Java is required for SikuliX (screen_*, ui_template_*, "
                "app_activate). Install with `brew install openjdk` and "
                "follow the post-install symlink hint brew prints. On a "
                "fresh Mac this is the most common cause of screen_* "
                "failures."
            ),
        }
    try:
        proc = subprocess.run(
            [java, "-version"], capture_output=True, text=True, timeout=5,
        )
        version = (proc.stderr or proc.stdout).strip().split("\n")[0]
        return {"available": True, "path": java, "version": version}
    except Exception as e:
        return {"available": True, "path": java, "version_error": str(e)}


def _walk_tcc_chain() -> dict[str, Any]:
    import os
    import subprocess

    chain = []
    current_pid = os.getppid()
    for _ in range(20):
        if current_pid <= 1:
            break
        try:
            proc = subprocess.run(
                ["ps", "-o", "pid=,ppid=,comm=", "-p", str(current_pid)],
                capture_output=True, text=True, timeout=3,
            )
            line = (proc.stdout or "").strip()
            if not line:
                break
            parts = line.split(None, 2)
            if len(parts) < 3:
                break
            pid_s, ppid_s, comm = parts[0], parts[1], parts[2]
            chain.append({"pid": int(pid_s), "name": comm})
            current_pid = int(ppid_s)
        except Exception:
            break

    likely = None
    for entry in chain:
        name = entry["name"]
        if ".app/" in name or name.endswith(".app"):
            likely = name
            break

    return {
        "chain": chain,
        "likely_tcc_responsible": likely,
        "hint": (
            f"macOS TCC attributes Accessibility/Screen Recording to the "
            f"responsible process up the launch chain — likely "
            f"`{likely}` here, NOT tai/tcsh. Grant THAT app the "
            f"permissions if screen_* tools fail at the TCC layer."
            if likely else
            "No .app ancestor found in the process chain (e.g., launched "
            "via ssh or cron). macOS may attribute permissions to the "
            "binary itself; check System Settings for entries matching "
            "`tai`/`tcsh`."
        ),
    }


def _check_window_listing() -> dict[str, Any]:
    try:
        from Quartz import (  # type: ignore[import-untyped]
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
    except Exception as e:
        return {"queryable": False, "error": f"Quartz import failed: {e}"}
    try:
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID,
        )
    except Exception as e:
        return {"queryable": True, "error": f"CGWindowListCopyWindowInfo failed: {e}"}

    total = len(windows)
    with_title = sum(1 for w in windows if w.get("kCGWindowName"))
    return {
        "queryable": True,
        "visible_windows": total,
        "with_readable_titles": with_title,
        "hint": (
            "Screen Recording permission appears granted "
            "(>=3 windows have readable titles)."
            if with_title >= 3 else
            "Few/no readable window titles — Screen Recording is "
            "likely DENIED for the responsible process. See tcc_chain."
        ),
    }


def _check_payload_state() -> dict[str, Any]:
    import os
    from pathlib import Path

    home = os.environ.get("HOME", "")
    cache_root = Path(home) / "Library" / "Caches" / "tai"
    caches = []
    if cache_root.exists():
        for p in sorted(cache_root.glob("payload-*")):
            if p.is_dir() and not p.name.endswith(".tmp-" + str(os.getpid())):
                stamp = p / ".stamp"
                caches.append({
                    "dir": str(p),
                    "ready": stamp.exists(),
                })
    return {"cache_root": str(cache_root), "extracted_caches": caches}


def _summarize(result: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not result["java"].get("available"):
        issues.append("Java is missing — install with `brew install openjdk`.")
    sr = result["screen_recording"]
    if sr.get("queryable") and sr.get("with_readable_titles", 0) < 3:
        issues.append(
            "Screen Recording permission appears denied for the TCC "
            "responsible process."
        )
    # The SikuliX jar + bridge script are read-only prerequisites:
    # if they're missing, the bundle's broken. HOLO_TEMPLATE_DIR is
    # a write-on-demand cache so non-existence at startup is fine.
    for name in ("HOLO_SIKULI_JAR", "HOLO_BRIDGE_SCRIPT"):
        info = result["env"].get(name, {})
        if info.get("set") and not info.get("exists"):
            issues.append(f"{name} points at a missing path: {info.get('value')}")
    return issues or ["All baseline checks pass."]


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
