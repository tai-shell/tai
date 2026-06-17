"""MCP server surface for the holo daemon.

Exposes the `Daemon` / `Channel` primitives as MCP tools so an AI
agent (Claude Code, Codex, Cursor, …) can drive the user's already-
signed-in browser tab through the bookmarklet channel.

This module is the thin in-process variant: each MCP server instance
owns one `Daemon`, started lazily on the first tool call that needs
it. The agent calibrates one or more tabs (`calibrate`), then issues
commands against them by sid (`ping`, `read_global`, `send_command`).
The set of available bookmarklet ops is whatever `bookmarklet/
dispatch.js` knows about today; `send_command` is the forward-compat
escape hatch for ops we add later.

Run via `holo mcp` (stdio transport) or import `build_server()` to
embed in another runner.
"""

from __future__ import annotations

import signal
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from holo.channel import CalibrationError, Channel, CommandError
from holo.daemon import Daemon
from holo.mcp_wire import WIRE_MAGIC, is_valid_handshake, read_handshake
from holo.templates import TemplateNotFound, TemplateStore


class HoloMCPServer:
    """Holds the lazy Daemon and the tool implementations.

    Splitting the tool bodies off the FastMCP decorator lets tests
    drive them directly without spinning up a stdio loop, and gives
    us one place to attach a fake daemon in tests.
    """

    def __init__(
        self,
        *,
        hide_qr: bool = False,
        enable_screen: bool = False,
        no_bookmarklet: bool = False,
        no_browser: bool = False,
        input_proxy: tuple[str, int] | None = None,
        remote_screen: tuple[str, int] | None = None,
        templates: TemplateStore | None = None,
        announce: bool = False,
        announce_session: str | None = None,
        announce_user: str | None = None,
        announce_ssh_user: str | None = None,
        announce_ips: list[str] | None = None,
        announce_port: int = 0,
        announce_capabilities: bool = False,
        announce_command: str | None = None,
        announce_resources: list[Any] | None = None,
        auto_tunnel: bool = False,
        auto_tunnel_backend: str | None = None,
    ) -> None:
        self.hide_qr = hide_qr
        self.enable_screen = enable_screen
        self.no_bookmarklet = no_bookmarklet
        self.no_browser = no_browser
        self.input_proxy = input_proxy
        self.remote_screen = remote_screen
        self._daemon: Daemon | None = None
        self._daemon_lock = threading.Lock()

        # Per-host resources for the Phase 2 exec primitive. Stored as a
        # name-keyed map so the holo_exec_in_resource MCP tool can look
        # them up cheaply. Empty when no --announce-resource was passed;
        # the tool is then NOT registered on the surface (see
        # build_server).
        from holo.announce import Resource as _Resource

        self._resources: tuple[_Resource, ...] = tuple(announce_resources or ())
        self._resource_by_name: dict[str, _Resource] = {
            r.name: r for r in self._resources
        }
        # Template cache lives across daemon restarts — it's a pure
        # filesystem store, not bound to any JVM/browser session.
        self.templates = templates if templates is not None else TemplateStore()

        # Capabilities HTTP server has to come up BEFORE the announcer
        # so we can include the bound port + auth token in the TXT
        # record. If announce isn't on, capabilities is meaningless
        # (no one would know how to find the URL or token), so we skip
        # the server too — the CLI rejects the combination up front.
        self._caps_server: Any | None = None
        caps_port: int | None = None
        caps_token: str | None = None
        if announce and announce_capabilities:
            try:
                from holo.capabilities import CapabilitiesProbe
                from holo.capabilities_server import CapabilitiesServer

                probe = CapabilitiesProbe()
                self._caps_server = CapabilitiesServer(
                    probe=probe,
                    resources=announce_resources,
                )
                self._caps_server.start()
                caps_port = self._caps_server.actual_port
                caps_token = self._caps_server.token
            except Exception as e:  # noqa: BLE001 — surface and continue
                print(
                    f"holo mcp: capabilities server failed ({e}); "
                    "continuing without it",
                    file=sys.stderr,
                    flush=True,
                )
                self._caps_server = None

        self._announcer: Any | None = None
        if announce:
            try:
                from holo.announce import HoloAnnouncer

                self._announcer = HoloAnnouncer(
                    session=announce_session,
                    user=announce_user,
                    ssh_user=announce_ssh_user,
                    port=announce_port,
                    ips=announce_ips,
                    caps_port=caps_port,
                    caps_token=caps_token,
                    remote_command=announce_command,
                    resources=announce_resources,
                )
                self._announcer.start()
            except Exception as e:  # noqa: BLE001 — surface and continue
                print(
                    f"holo mcp: mDNS announce failed ({e}); continuing "
                    "without broadcast",
                    file=sys.stderr,
                    flush=True,
                )
                self._announcer = None

        # Long-lived LAN discovery cache. Always on, regardless of
        # `--announce` — even non-announcing holos want to be able to
        # answer "what other holos are around?" for the agent's
        # `holo_discover_sessions` / `holo_fetch_capabilities` tools.
        # mDNS startup failure logs and downgrades to "tools return
        # empty / raise"; it should never kill the MCP server.
        self._discover: Any | None = None
        try:
            from holo.discover import DiscoverHandle

            self._discover = DiscoverHandle()
            self._discover.start()
        except Exception as e:  # noqa: BLE001 — surface and continue
            print(
                f"holo mcp: LAN discovery cache failed ({e}); "
                "holo_discover_sessions will return empty and "
                "holo_fetch_capabilities will raise",
                file=sys.stderr,
                flush=True,
            )
            self._discover = None

        # CloudCity reverse-tunnel state (Phase 4 of the spec). Owned
        # at server scope so multiple `holo_tunnel_up` calls coexist
        # cleanly, and so the tunnel survives across MCP requests.
        # The lock guards against two concurrent tool invocations
        # racing each other into start/stop.
        self._tunnel: Any | None = None
        self._tunnel_lock = threading.Lock()

        # SikuliX IDE process (Phase: `holo_launch_ide` tool). Tracked
        # at server scope so re-invocation while the IDE is still up
        # returns the existing PID instead of stacking multiple JVMs.
        # Spawned as a CHILD of the MCP server (not detached) so the
        # macOS Accessibility / Screen-Recording grant on the `holo`
        # binary covers the IDE's java.awt.Robot calls — detaching
        # via start_new_session would force a separate TCC prompt for
        # `java` and silently break mouse simulation until the user
        # granted it.
        self._ide_proc: Any | None = None
        self._ide_lock = threading.Lock()

        # Auto-tunnel watcher (Phase 5b). When enabled, opens one
        # reverse tunnel per discovered CloudCity and keeps the
        # session announce's tunnel_ports map in sync. Skipped
        # without --announce because there's no announcer to update.
        self._auto_tunnel: Any | None = None
        if auto_tunnel and self._announcer is not None:
            try:
                from holo.auto_tunnel import AutoTunnel

                self._auto_tunnel = AutoTunnel(
                    announcer=self._announcer,
                    backend=auto_tunnel_backend,
                )
                self._auto_tunnel.start()
            except Exception as e:  # noqa: BLE001 — surface and continue
                print(
                    f"holo mcp: --auto-tunnel watcher failed ({e}); "
                    "continuing without auto-tunnel — manual "
                    "holo_tunnel_up still works",
                    file=sys.stderr,
                    flush=True,
                )
                self._auto_tunnel = None
        elif auto_tunnel and self._announcer is None:
            print(
                "holo mcp: --auto-tunnel requires --announce; ignoring",
                file=sys.stderr,
                flush=True,
            )

    @property
    def daemon(self) -> Daemon:
        with self._daemon_lock:
            if self._daemon is None:
                self._daemon = Daemon(
                    hide_qr=self.hide_qr,
                    enable_screen=self.enable_screen,
                    no_bookmarklet=self.no_bookmarklet,
                    input_proxy=self.input_proxy,
                    remote_screen=self.remote_screen,
                )
            return self._daemon

    def shutdown(self) -> None:
        # Tear down the reverse-tunnel before the announcer so listening
        # companions get the Goodbye after the tunnel is already gone —
        # otherwise they could briefly see `tunnel_port` advertised
        # against a dead ssh process.
        if self._auto_tunnel is not None:
            try:
                self._auto_tunnel.stop()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
            self._auto_tunnel = None
        with self._tunnel_lock:
            if self._tunnel is not None:
                try:
                    self._tunnel.stop()
                except Exception:  # noqa: BLE001 — shutdown must not raise
                    pass
                self._tunnel = None
        with self._ide_lock:
            if self._ide_proc is not None:
                try:
                    if self._ide_proc.poll() is None:
                        self._ide_proc.terminate()
                        try:
                            self._ide_proc.wait(timeout=3.0)
                        except Exception:  # noqa: BLE001
                            self._ide_proc.kill()
                except Exception:  # noqa: BLE001 — shutdown must not raise
                    pass
                self._ide_proc = None
        # Stop the announcer first so the LAN sees a Goodbye while the
        # capabilities server is still up — minimizes the window where
        # a discoverer could see the broadcast but get a connection
        # refused on the caps URL.
        if self._announcer is not None:
            try:
                self._announcer.stop()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
            self._announcer = None
        if self._caps_server is not None:
            try:
                self._caps_server.stop()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
            self._caps_server = None
        if self._discover is not None:
            try:
                self._discover.stop()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
            self._discover = None
        with self._daemon_lock:
            if self._daemon is not None:
                self._daemon.shutdown()
                self._daemon = None

    # ---- tool implementations ---------------------------------------

    def calibrate(self, timeout: float = 30.0) -> dict[str, Any]:
        """Return the most recently registered channel if one exists,
        otherwise wait for a fresh calibration beacon.

        The fast-path matters for cross-host setups: the human
        calibrates locally on the daemon's machine (where the browser
        is), then a remote agent connecting in shouldn't have to
        re-trigger the bookmarklet — it can just keep working with
        whatever's already registered.
        """
        existing = self.daemon.registry.items()
        if existing:
            _, ch = existing[-1]
            return _describe(ch)
        try:
            ch = self.daemon.calibrate(timeout=timeout)
        except CalibrationError as e:
            raise RuntimeError(f"calibration timeout: {e}") from e
        return _describe(ch)

    def list_channels(self) -> dict[str, Any]:
        """Snapshot of currently registered channels."""
        return {"channels": [_describe(ch) for _, ch in self.daemon.registry.items()]}

    def drop_channel(self, sid: str) -> dict[str, Any]:
        """Forget the channel for `sid`. Does not close the browser popup."""
        ch = self.daemon.registry.unregister(sid)
        if ch is None:
            raise ValueError(f"no channel for sid {sid!r}")
        return {"ok": True, "sid": sid}

    def ping(self, sid: str, timeout: float = 5.0) -> dict[str, Any]:
        """Round-trip a ping to confirm the channel is live."""
        return self.send_command(sid, {"op": "ping"}, timeout=timeout)

    def read_global(
        self, sid: str, path: str, timeout: float = 5.0
    ) -> dict[str, Any]:
        """Read a dotted path off the page's global object."""
        if not path:
            raise ValueError("path must be non-empty")
        return self.send_command(
            sid, {"op": "read_global", "path": path}, timeout=timeout
        )

    def send_command(
        self, sid: str, command: dict[str, Any], timeout: float = 5.0
    ) -> dict[str, Any]:
        """Send an arbitrary command to the bookmarklet for `sid`.

        `command` must be a JSON-serialisable dict with an `op` field.
        """
        if not isinstance(command, dict):
            raise ValueError("command must be an object")
        if not isinstance(command.get("op"), str) or not command["op"]:
            raise ValueError("command must have a non-empty string `op` field")
        ch = self._require_channel(sid)
        try:
            result = ch.send_command(command, timeout=timeout)
        except CommandError as e:
            raise RuntimeError(f"command failed: {e}") from e
        return {
            "sid": sid,
            "transport": _transport(ch),
            "result": result,
        }

    def _require_channel(self, sid: str) -> Channel:
        ch = self.daemon.registry.lookup(sid)
        if ch is None:
            raise ValueError(f"no channel for sid {sid!r}")
        return ch

    # ---- screen / SikuliX tools (no sid; drives whatever's foreground) ----

    def _require_bridge(self) -> Any:
        """Return the daemon's SikuliX bridge or raise a clean error.

        Used by capture-only paths (screenshot, find_image, user_capture)
        that must run on this host. Input ops use :meth:`_input_target`
        instead — they can route to a remote daemon via `--input-proxy`.
        """
        bridge = self.daemon.bridge
        if bridge is None:
            raise RuntimeError(
                "Screen tools unavailable. Start the daemon with "
                "`enable_screen=True` (CLI: `holo mcp --screen`) and "
                "ensure OpenJDK 11+ + sikulix*.jar are installed."
            )
        return bridge

    def _input_target(self) -> Any:
        """Return the backend for input ops (click / key / type / scroll
        / activate).

        Prefers the remote input proxy when `--input-proxy HOST:PORT`
        (or `--remote-input`) is set: corporate-locked machines can
        capture their own screen but can't inject events, and the proxy
        points at a peer holo on a machine that can. Falls back to the
        local SikuliX bridge otherwise. The two backends present
        identical input signatures, so the call sites are unchanged.
        """
        daemon = self.daemon
        remote = getattr(daemon, "_remote_input", None)
        if remote is not None:
            return remote
        return self._require_bridge()

    def _capture_target(self) -> Any:
        """Return the backend for screen-capture ops (screenshot /
        find_image / find_image_path / user_capture).

        Prefers the remote-screen proxy when `--remote-screen HOST:PORT`
        is set: useful when this host can't do screen capture (e.g.
        Claude Code is the responsible TCC process and lacks Screen
        Recording) but a peer can — typically a peer that mirrors this
        host's display via Screen Sharing, so a capture on the peer
        actually shows the local user's working content.

        Symmetric to `_input_target`. When both proxies target the same
        host, the same backend instance is returned for both — see
        ``Daemon._backends`` for the sharing logic.
        """
        daemon = self.daemon
        remote = getattr(daemon, "_remote_screen", None)
        if remote is not None:
            return remote
        return self._require_bridge()

    def app_activate(self, name: str) -> dict[str, Any]:
        """Bring an application to the foreground by name."""
        return self._input_target().activate(name)

    def screen_click(
        self, x: int, y: int, modifiers: list[str] | None = None
    ) -> dict[str, Any]:
        """Click at screen coordinates, optionally holding modifiers."""
        return self._input_target().click(x, y, modifiers=modifiers or [])

    def screen_type(self, text: str) -> dict[str, Any]:
        """Type a literal string into whatever has keyboard focus."""
        return self._input_target().type_text(text)

    def screen_key(self, combo: str) -> dict[str, Any]:
        """Send a key combo, e.g. 'cmd+v', 'enter', 'shift+tab'."""
        return self._input_target().key(combo)

    def screen_scroll(
        self,
        x: int,
        y: int,
        direction: str = "down",
        steps: int = 3,
    ) -> dict[str, Any]:
        """Move to (x, y) and emit `steps` mouse-wheel events."""
        return self._input_target().scroll(
            x, y, direction=direction, steps=steps
        )

    def screen_move(self, x: int, y: int) -> dict[str, Any]:
        """Move the cursor to (x, y) — no click, no scroll, no key."""
        return self._input_target().mouse_move(x, y)

    def screen_shot(
        self, region: dict[str, int] | None = None
    ) -> dict[str, Any]:
        """Capture the screen (or a region) and return base64 PNG + size."""
        import base64 as _b64

        png = self._capture_target().screenshot(region=region)
        return {
            "image": _b64.b64encode(png).decode("ascii"),
            "format": "png",
            "byte_count": len(png),
        }

    def screen_find_image(
        self,
        needle: str,
        region: dict[str, int] | None = None,
        score: float = 0.7,
    ) -> dict[str, Any] | None:
        """Find `needle` (base64-encoded PNG) on screen. Returns coords or null."""
        import base64 as _b64

        try:
            needle_bytes = _b64.b64decode(needle, validate=True)
        except Exception as e:
            raise ValueError("needle must be base64-encoded PNG") from e
        return self._capture_target().find_image(
            needle_bytes, region=region, score=score
        )

    def screen_user_capture(
        self, prompt: str = "", timeout: float = 60.0
    ) -> dict[str, Any]:
        """Run the interactive drag-rectangle capture; return the dragged
        rect's PNG (or a cancel record).

        Exposed as a top-level tool primarily so `--remote-screen`'s
        ``RemoteHoloBackend.user_capture`` has something to call on the
        peer. Local callers normally hit this through
        ``ui_template_capture`` instead.
        """
        return self._capture_target().user_capture(prompt=prompt, timeout=timeout)

    # ---- UI template cache ----------------------------------------------
    #
    # Maps natural-language `(app, label)` keys to stored PNG variants
    # so the agent doesn't need to re-discover the same on-screen
    # element via Vision every session. `_capture` blocks for a user
    # rectangle drag (or accepts a `region` for programmatic stash);
    # `_find` runs SikuliX template matching against the saved PNG;
    # `_click` is the find-and-click convenience over the existing
    # screen.click. Storage layout / index format documented in
    # `holo.templates`.

    def ui_template_capture(
        self,
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
        replace: bool = False,
        similarity: float = 0.85,
        timeout: float = 60.0,
        prompt: str = "",
    ) -> dict[str, Any]:
        """Save a template for `(app, label)` from a user-drawn rect or a programmatic region.

        - region=None → blocks for a user `drag-rectangle` capture (Esc cancels).
        - region={x,y,w,h} → captures that rect via screen.shot (no UI prompt).

        `replace=True` discards prior variants for the entry; otherwise the
        new image is appended as another variant (idle / hover / etc.).
        Returns the saved index entry.
        """
        capture = self._capture_target()
        if region is not None:
            png = capture.screenshot(region=region)
        else:
            result = capture.user_capture(prompt=prompt, timeout=timeout)
            if result.get("cancelled"):
                # Surface as a non-error response — agent re-prompts user.
                return {
                    "cancelled": True,
                    "reason": result.get("reason", "user cancelled"),
                }
            import base64 as _b64

            png = _b64.b64decode(result["image"])
        entry = self.templates.add_variant(
            label, app, png, replace=replace, similarity=similarity
        )
        return {"saved": True, "entry": entry}

    def ui_template_list(self, app: str | None = None) -> dict[str, Any]:
        """List stored templates. `app=None` lists all; pass `'_global'` for the catch-all."""
        return {"templates": self.templates.list(app=app)}

    def ui_template_find(
        self,
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Locate a saved template on the current screen.

        Walks each variant in order and returns the first hit. Bumps
        `last_used` / `match_count` on the entry. Returns None if no
        variant matches anywhere on the (optionally constrained) screen.

        Raises `LookupError` if there's no entry for `(app, label)` —
        distinct from "entry exists but doesn't match right now".
        """
        try:
            paths = self.templates.variant_paths(label, app)
        except TemplateNotFound as e:
            raise LookupError(str(e)) from e
        entry = self.templates.get(label, app)
        # `get` is checked again because `variant_paths` raises before
        # we reach this; we know it exists.
        score = float(entry["similarity"]) if entry else 0.85
        capture = self._capture_target()
        for p in paths:
            match = capture.find_image_path(str(p), region=region, score=score)
            if match is not None:
                self.templates.touch(label, app)
                return {**match, "variant": p.name}
        return None

    def ui_template_click(
        self,
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
        button: str = "left",
        clicks: int = 1,
    ) -> dict[str, Any]:
        """Find a saved template and click its center. Raises if nothing matches.

        Convenience over `ui_template_find` + `screen_click`. The agent
        gets one tool call for the 80% case (open the bookmarks menu,
        click a saved icon, etc.). On miss this raises rather than
        silently doing nothing — clicking the wrong place is worse than
        a clear error.
        """
        del button, clicks  # not yet wired through screen.click — left/single only
        match = self.ui_template_find(label, app, region=region)
        if match is None:
            app_norm = app or "_global"
            raise RuntimeError(
                f"template {app_norm}/{label} matched nothing on screen "
                "(use ui_template_capture to refresh, or check whether the "
                "target app is in front)"
            )
        cx = int(match["x"] + match["width"] / 2)
        cy = int(match["y"] + match["height"] / 2)
        self._input_target().click(cx, cy)
        return {
            "clicked": True,
            "x": cx,
            "y": cy,
            "score": match["score"],
            "variant": match["variant"],
        }

    def ui_template_delete(
        self,
        label: str,
        app: str | None = None,
        variant: str | None = None,
    ) -> dict[str, Any]:
        """Remove a stored template entry, or just one of its variants."""
        removed = self.templates.delete(label, app, variant=variant)
        return {"removed": removed}

    # ---- Chrome browser tools (AppleScript; macOS-only) -----------------
    #
    # These bypass the SikuliX keystroke layer entirely — Chrome's
    # AppleScript dictionary is synchronous and reliable, no focus
    # races, no beeps. Use these instead of `app_activate` +
    # `screen_key cmd+l` + `screen_type` + `screen_key enter` for any
    # browser navigation. The bookmarklet channel is still the right
    # tool for in-page DOM reads.

    def browser_navigate(self, url: str) -> dict[str, Any]:
        """Set the active tab's URL (Chrome, front window)."""
        from holo import browser_chrome

        try:
            return browser_chrome.navigate(url)
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_new_tab(self, url: str | None = None) -> dict[str, Any]:
        """Open a new tab in Chrome's front window. URL is optional."""
        from holo import browser_chrome

        try:
            return browser_chrome.new_tab(url)
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_close_active_tab(self) -> dict[str, Any]:
        """Close the active tab of Chrome's front window."""
        from holo import browser_chrome

        try:
            return browser_chrome.close_active_tab()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_activate_tab(self, index: int) -> dict[str, Any]:
        """Make tab `index` (1-based) the active tab of the front window."""
        from holo import browser_chrome

        try:
            return browser_chrome.activate_tab(index)
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_list_tabs(self) -> dict[str, Any]:
        """List tabs in the front window: `{tabs: [{id,title,url,index}], active: index}`."""
        from holo import browser_chrome

        try:
            return browser_chrome.list_tabs()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_read_active_url(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.read_active_url()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_read_active_title(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.read_active_title()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_reload(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.reload()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_back(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.go_back()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_forward(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.go_forward()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_execute_js(self, js: str) -> dict[str, Any]:
        """Run a JS expression in Chrome's active tab via AppleScript.

        Requires Chrome's 'Allow JavaScript from Apple Events' toggle
        (View → Developer). When that's off, raises a runtime error
        whose message tells the caller exactly how to enable it OR to
        fall back to `bookmarklet_query` against a calibrated channel.
        """
        from holo import browser_chrome

        try:
            return browser_chrome.execute_js(js)
        except browser_chrome.JavaScriptNotAuthorized as e:
            # Specific message; the agent can read this and route to
            # `bookmarklet_query` if a channel is available.
            raise RuntimeError(str(e)) from e
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def bookmarklet_query(
        self,
        sid: str,
        selector: str,
        prop: str = "innerText",
        attr: str | None = None,
        all: bool = False,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """DOM query through the bookmarklet channel — CSP-safe
        fallback when `browser_execute_js` is unavailable.

        - `selector` is a CSS selector
        - `prop` is the JS property to read (default 'innerText'); ignored when `attr` is set
        - `attr` is an HTML attribute name; takes precedence over `prop`
        - `all=True` returns a list of matches; default returns the first match
        """
        if not selector:
            raise ValueError("selector must be non-empty")
        cmd: dict[str, Any] = {
            "op": "query_selector_all" if all else "query_selector",
            "selector": selector,
        }
        if attr is not None:
            cmd["attr"] = attr
        else:
            cmd["prop"] = prop
        return self.send_command(sid, cmd, timeout=timeout)

    # ---- LAN session discovery / capabilities --------------------------------
    #
    # Lets an agent enumerate other `holo mcp --announce` sessions on the
    # same broadcast domain and fetch their hardware/software/package
    # inventories — the read-side of the `--announce-capabilities` feature.
    # Routing decisions ("send transcription to the M4, not the M1"; "find
    # a host with Chrome Canary installed") happen agent-side once these
    # tools return.

    def holo_discover_sessions(self, wait_s: float = 0.0) -> dict[str, Any]:
        """Return live `_holo-session._tcp.local.` broadcasts from cache.

        The HoloMCPServer keeps a long-lived zeroconf browser running
        (see `DiscoverHandle`), so this call returns instantly with
        whatever the cache has seen — no per-call browse delay. The
        cache is continuously refreshed as new announce broadcasts
        arrive and old ones expire (stale-swept at 2× the mDNS TTL).

        ``wait_s`` is an optional grace period: pass a small value
        (e.g. 1.5 s) if you just told the user to start a new daemon
        and want to give it time to land in the cache before
        snapshotting. Default 0 — the cache is usually already fresh.

        Each session includes `caps_port` + `caps_token` when the
        broadcaster used `--announce-capabilities`; pass `instance`
        (or `session`/`host`) into `holo_fetch_capabilities` to read
        its inventory.

        mDNS is link-local — sessions on the other side of a router
        or most VPNs won't appear. For cross-network discovery, use
        the `holo connect HOST:PORT` flow instead.
        """
        if self._discover is None:
            return {"sessions": [], "count": 0}
        if wait_s > 0:
            import time as _time

            _time.sleep(float(wait_s))
        sessions = self._discover.snapshot()
        return {"sessions": sessions, "count": len(sessions)}

    def holo_fetch_capabilities(
        self,
        instance: str,
        timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        """Fetch a remote holo host's hardware/software/package inventory.

        Reads the instance from the live discovery cache (no per-call
        zeroconf browse — see `DiscoverHandle`), then HTTP-fetches its
        `/capabilities` endpoint with the broadcast token.

        ``instance`` is matched against (in order) the mDNS instance
        label, the ``session`` field, then the ``host`` field — pass
        whatever the user typed and we'll find it. The remote must
        have been launched with `--announce-capabilities`; otherwise
        we raise rather than silently return an empty inventory.

        Advertised IPs are tried in order with a per-attempt timeout
        (the first IP can be a VPN tunnel address that isn't reachable
        from the discoverer). The first successful fetch wins; if none
        reach, raises with the list of attempts.

        If the target session isn't in the cache yet, raises a clear
        error — the agent can call `holo_discover_sessions(wait_s=1.5)`
        first to give a freshly-started daemon time to appear.
        """
        import json as _json
        import urllib.error
        import urllib.request

        from holo.capabilities_server import CAPS_TOKEN_HEADER

        if self._discover is None:
            raise RuntimeError(
                "discover cache unavailable — mDNS browser failed to start; "
                "no LAN sessions are visible to this MCP server"
            )

        sessions = self._discover.snapshot()

        target = _match_session(sessions, instance)
        if target is None:
            seen = [s.get("instance") for s in sessions]
            raise RuntimeError(
                f"no holo session matching {instance!r} on the LAN; "
                f"saw: {seen!r}"
            )

        caps_port = target.get("caps_port")
        caps_token = target.get("caps_token")
        if not caps_port or not caps_token:
            raise RuntimeError(
                f"session {target.get('instance')!r} has no capabilities "
                "endpoint — was it launched with `--announce-capabilities`?"
            )

        ips = target.get("ips") or []
        if not ips:
            raise RuntimeError(
                f"session {target.get('instance')!r} broadcast no IPs; "
                "nothing to fetch from"
            )

        attempts: list[dict[str, str]] = []
        for ip in ips:
            url = f"http://{ip}:{caps_port}/capabilities"
            req = urllib.request.Request(
                url, headers={CAPS_TOKEN_HEADER: caps_token}
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    body = _json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                attempts.append({"ip": ip, "error": str(e)})
                continue
            return {
                "instance": target.get("instance"),
                "host": target.get("host"),
                "session": target.get("session"),
                "ip_used": ip,
                "capabilities": body,
            }

        raise RuntimeError(
            f"could not reach capabilities endpoint for "
            f"{target.get('instance')!r}; tried: {attempts!r}"
        )

    # ---- CloudCity reverse-tunnel ------------------------------------
    #
    # Phase 4 of the CloudCity tunnel spec. The desktop SPA invokes
    # `holo_tunnel_up` to bring up a reverse SSH forward into one of
    # the LAN's CloudCity hosts. Once the tunnel is up, this daemon's
    # mDNS announcement (the existing `_holo-session._tcp.local.`
    # record) gains a `tunnel_port=<N>` field so SPAs that key off
    # discovery rather than off the MCP response also see the new
    # routing. `holo_tunnel_down` tears the tunnel down explicitly.

    def holo_tunnel_up(
        self,
        cloudcity_instance: str,
        backend: str | None = None,
        force_cert_refresh: bool = False,
    ) -> dict[str, Any]:
        """Open a reverse SSH tunnel to a CloudCity host.

        Picks the named CloudCity from the LAN discovery cache,
        ensures the daemon's outbound-SSH cert is fresh, then runs
        ``ssh -A -N -R 0:localhost:22 lando@<cc-ip>:<cc-port>`` so
        c2w-net's loopback gains a port that forwards back to this
        daemon's :22. Returns the allocated tunnel port plus the
        target metadata.

        Idempotent at the server level: a second call replaces any
        existing tunnel (the previous ssh subprocess is stopped before
        the new one starts) — the desktop SPA can call this on every
        click without bookkeeping.

        ``cloudcity_instance`` matches against (in order) the mDNS
        instance label, then ``host``. Use ``holo cloudcity discover
        --json`` (or read the future `/cloudcities` endpoint) to list
        candidates.

        ``backend`` selects where the cert refresh hits:
            1. explicit value → wins
            2. otherwise: ``HOLO_BACKEND`` env var, then the build-time
               default. See ``holo.cert.resolve_backend``.

        ``force_cert_refresh=True`` re-fetches the cert even if the
        on-disk one is still valid — useful when rotating CAs.

        After the tunnel is up, the existing ``_holo-session._tcp.``
        announce is updated to carry ``tunnel_port=<N>``.
        """
        from holo import cloudcity_announce as cc_announce
        from holo import tunnel as tunnel_mod

        with self._tunnel_lock:
            # Replace any existing tunnel first so the second call
            # doesn't leak the prior ssh subprocess.
            if self._tunnel is not None:
                try:
                    self._tunnel.stop()
                except Exception:  # noqa: BLE001 — log + keep going
                    pass
                self._tunnel = None
                if self._announcer is not None:
                    try:
                        self._announcer.set_tunnel_port(None)
                    except Exception:  # noqa: BLE001
                        pass

            if self._discover is None:
                raise RuntimeError(
                    "discover cache unavailable — mDNS browser failed "
                    "to start; cannot resolve CloudCity by instance"
                )
            # Phase 4b doesn't consume the CloudCity browser through
            # the running DiscoverHandle (that's session-only). Spin
            # up a short-lived browse via tunnel.find_cloudcity and
            # surface a clear error if nothing matches.
            record = tunnel_mod.find_cloudcity(
                cloudcity_instance, wait_s=1.5
            )
            if record is None:
                raise RuntimeError(
                    f"no CloudCity matching {cloudcity_instance!r} on "
                    f"the LAN. Is `holo cloudcity announce` running on "
                    f"the host?"
                )

            tunnel = tunnel_mod.open_to_cloudcity(
                record,
                backend=backend,
                cert_force_refresh=force_cert_refresh,
            )
            self._tunnel = tunnel

            # Mirror the new port onto the session announcement so
            # SPAs that key off discovery (rather than off this MCP
            # response) see the change. No-op when the announcer is
            # disabled (`holo mcp` without `--announce`).
            if self._announcer is not None:
                try:
                    self._announcer.set_tunnel_port(tunnel.port)
                except Exception:  # noqa: BLE001 — log + keep going
                    pass

            target = tunnel.target
            return {
                "tunnel_port": tunnel.port,
                "cloudcity_instance": record.get("instance"),
                "cloudcity_host": record.get(cc_announce.FIELD_HOST),
                "cloudcity_target": (
                    f"{target[0]}:{target[1]}" if target else None
                ),
                "cert_refreshed": force_cert_refresh,
            }

    def holo_tunnel_down(self) -> dict[str, Any]:
        """Tear down the active reverse tunnel, if any.

        Always returns successfully — calling this when no tunnel is
        up is a no-op (returns ``{"closed": False}``). Also clears
        ``tunnel_port`` from the session announcement.
        """
        with self._tunnel_lock:
            if self._tunnel is None:
                return {"closed": False}
            try:
                self._tunnel.stop()
            except Exception:  # noqa: BLE001 — surface once we're stable
                pass
            self._tunnel = None
            if self._announcer is not None:
                try:
                    self._announcer.set_tunnel_port(None)
                except Exception:  # noqa: BLE001
                    pass
            return {"closed": True}

    def holo_launch_ide(self) -> dict[str, Any]:
        """Launch the SikuliX IDE as a child of this MCP server.

        Idempotent at server scope: if a previous launch is still
        running, returns its PID with ``already_running=True`` instead
        of stacking multiple JVMs. Returns immediately — the IDE is a
        long-lived GUI process, not awaited.

        Returns
        -------
        ``{"pid": int, "jar": str, "java": str, "already_running": bool}``
        """
        import shutil
        import subprocess

        from holo.bridge import BridgeMissingError, ensure_jar

        with self._ide_lock:
            if self._ide_proc is not None and self._ide_proc.poll() is None:
                return {
                    "pid": self._ide_proc.pid,
                    "jar": getattr(self._ide_proc, "_holo_jar", ""),
                    "java": getattr(self._ide_proc, "_holo_java", ""),
                    "already_running": True,
                }

            java = shutil.which("java")
            if java is None:
                raise RuntimeError(
                    "holo_launch_ide: `java` not on PATH. Install "
                    "OpenJDK 11+ (e.g. `brew install openjdk@21`) on "
                    "the host running the holo daemon."
                )
            try:
                jar_path = ensure_jar()
            except BridgeMissingError as e:
                raise RuntimeError(f"holo_launch_ide: {e}") from e

            argv = [java, "-jar", str(jar_path)]
            # No start_new_session: keep the JVM in holo's process tree
            # so macOS TCC stays attributed to the `holo` binary. The
            # IDE will be HUP'd if the MCP server exits — acceptable
            # because the typical flow is "agent opens IDE, user uses
            # it inside the same Claude Code session, user closes IDE
            # or session". stdin/stdout/stderr inherit from holo —
            # SikuliX's GUI noise goes wherever holo's stderr does.
            proc = subprocess.Popen(argv)
            proc._holo_jar = str(jar_path)  # type: ignore[attr-defined]
            proc._holo_java = java  # type: ignore[attr-defined]
            self._ide_proc = proc
            return {
                "pid": proc.pid,
                "jar": str(jar_path),
                "java": java,
                "already_running": False,
            }

    def list_resources(self) -> dict[str, Any]:
        """Return all resources declared on this daemon.

        Mirrors ``GET /v1/resources`` (capabilities_server) but
        reached over the MCP channel rather than a separate HTTPS
        roundtrip. Tai's ``on`` keyword dispatch uses this — the
        auto-tunnel is already authed and warm; a parallel HTTP call
        would duplicate trust machinery.

        Returns ``{"resources": [{name, path, tags, caps,
        allow_principals}, ...]}``. Empty list when no resources are
        declared (the tool is registered only when at least one is,
        so this only fires for declared daemons).
        """
        return {
            "resources": [
                {
                    "name": r.name,
                    "path": r.path,
                    "tags": list(r.tags),
                    "caps": list(r.caps),
                    # Surfaced for tools that want to render the
                    # intended ACL. NOT enforced in v1 — see Resource.
                    "allow_principals": list(r.allow_principals),
                }
                for r in self._resources
            ],
        }

    def exec_in_resource(
        self,
        resource: str,
        body: str,
        env: dict[str, str] | None = None,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        """Phase 2.A exec primitive — validate, spawn, batch output frames.

        Looks up the resource by name on this daemon's announced
        resources, runs the body through
        :func:`holo.resources_exec.exec_in_resource`, and returns a
        single dict with the collected ``frames`` + terminal status.

        Failure modes (returned as ``{"error": "...", ...}``, never
        raised — MCP clients see a structured response either way):

          - ``unknown-resource``: ``resource`` doesn't match any announced
            resource on this daemon. Response includes the known names.
          - ``body-rejected``: static parse failed (disallowed command,
            absolute path, traversal). ``message`` names the offender.
          - ``exec-setup``: a declared cap binary isn't on the daemon's
            PATH at call time, or the resource path doesn't exist.

        Success returns ``{"frames": [...], "exit": int, "duration_ms":
        int, "timed_out": bool}`` where ``frames`` is a list of
        ``{"fd": "stdout"|"stderr", "data": str}`` entries in arrival
        order across both streams.
        """
        from holo.resources_exec import BodyRejected
        from holo.resources_exec import exec_in_resource as _exec

        r = self._resource_by_name.get(resource)
        if r is None:
            return {
                "error": "unknown-resource",
                "resource": resource,
                "known": sorted(self._resource_by_name),
            }
        frames: list[dict[str, Any]] = []
        try:
            result = _exec(
                r,
                body,
                env=env,
                timeout_s=timeout_s,
                on_frame=frames.append,
            )
        except BodyRejected as e:
            return {"error": "body-rejected", "message": str(e)}
        except FileNotFoundError as e:
            return {"error": "exec-setup", "message": str(e)}
        return {
            "frames": frames,
            "exit": result.exit_code,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
        }


def _match_session(
    sessions: list[dict[str, Any]], identifier: str
) -> dict[str, Any] | None:
    """Find the session whose instance / session / host matches ``identifier``.

    Match priority is exact-instance > exact-session > exact-host so a
    unique mDNS label always wins over a possibly-duplicated session
    name. Returns None if nothing matches — the caller raises with a
    helpful list-of-instances message.
    """
    for s in sessions:
        if s.get("instance") == identifier:
            return s
    for s in sessions:
        if s.get("session") == identifier:
            return s
    for s in sessions:
        if s.get("host") == identifier:
            return s
    return None


def _transport(ch: Channel) -> str:
    return "ws" if ch._ws_ready else "qr"


def _describe(ch: Channel) -> dict[str, Any]:
    return {
        "sid": ch.session,
        "window_id": ch._window_id,
        "window_owner": ch._window_owner,
        "transport": _transport(ch),
    }


def build_server(
    *,
    hide_qr: bool = False,
    enable_screen: bool = False,
    no_bookmarklet: bool = False,
    no_browser: bool = False,
    input_proxy: tuple[str, int] | None = None,
    remote_screen: tuple[str, int] | None = None,
    announce: bool = False,
    announce_session: str | None = None,
    announce_user: str | None = None,
    announce_ssh_user: str | None = None,
    announce_ips: list[str] | None = None,
    announce_port: int = 0,
    announce_capabilities: bool = False,
    announce_command: str | None = None,
    announce_resources: list[Any] | None = None,
    auto_tunnel: bool = False,
    auto_tunnel_backend: str | None = None,
) -> tuple[FastMCP, HoloMCPServer]:
    """Build a FastMCP instance with the holo tools registered.

    Returns the FastMCP server and the underlying `HoloMCPServer` so
    the caller can shut down the daemon after `mcp.run()` returns.

    With `no_bookmarklet=True`, the channel-dependent tools
    (calibrate, list_channels, drop_channel, ping, read_global,
    send_command, bookmarklet_query) are not registered and the
    daemon never spins up its WS server. Suits agents that only
    drive screen + AppleScript surfaces.

    With `announce=True`, broadcasts an mDNS service record
    (`_holo-session._tcp.local.`) carrying the optional session /
    user / ssh_user metadata for a companion desktop app to
    discover.
    """
    holo = HoloMCPServer(
        hide_qr=hide_qr,
        enable_screen=enable_screen,
        no_bookmarklet=no_bookmarklet,
        no_browser=no_browser,
        input_proxy=input_proxy,
        remote_screen=remote_screen,
        announce=announce,
        announce_session=announce_session,
        announce_user=announce_user,
        announce_ssh_user=announce_ssh_user,
        announce_ips=announce_ips,
        announce_port=announce_port,
        announce_capabilities=announce_capabilities,
        announce_command=announce_command,
        announce_resources=announce_resources,
        auto_tunnel=auto_tunnel,
        auto_tunnel_backend=auto_tunnel_backend,
    )
    mcp = FastMCP("holo")

    if not no_bookmarklet:
        @mcp.tool(
            description=(
                "Wait for the bookmarklet's calibration beacon and "
                "register a channel."
            )
        )
        def calibrate(timeout: float = 30.0) -> dict[str, Any]:
            return holo.calibrate(timeout=timeout)

        @mcp.tool(description="List currently registered channels (one per calibrated tab).")
        def list_channels() -> dict[str, Any]:
            return holo.list_channels()

        @mcp.tool(description="Forget a channel by sid. Does not close the browser popup.")
        def drop_channel(sid: str) -> dict[str, Any]:
            return holo.drop_channel(sid)

        @mcp.tool(description="Round-trip a ping through the channel for `sid`.")
        def ping(sid: str, timeout: float = 5.0) -> dict[str, Any]:
            return holo.ping(sid, timeout=timeout)

        @mcp.tool(
            description=(
                "Read a dotted path off the page's global object "
                "(e.g. 'document.title' or 'R2D2_VERSION')."
            )
        )
        def read_global(sid: str, path: str, timeout: float = 5.0) -> dict[str, Any]:
            return holo.read_global(sid, path, timeout=timeout)

        @mcp.tool(
            description=(
                "Send an arbitrary command to the bookmarklet. "
                "Must be a dict with a string `op` field; see bookmarklet/dispatch.js."
            )
        )
        def send_command(
            sid: str, command: dict[str, Any], timeout: float = 5.0
        ) -> dict[str, Any]:
            return holo.send_command(sid, command, timeout=timeout)

    @mcp.tool(description="Bring an application to the foreground by name (e.g. 'Google Chrome').")
    def app_activate(name: str) -> dict[str, Any]:
        return holo.app_activate(name)

    @mcp.tool(
        description=(
            "Click at screen coordinates (top-left origin). "
            "`modifiers` is an optional list like ['cmd'] or ['shift', 'ctrl']."
        )
    )
    def screen_click(
        x: int, y: int, modifiers: list[str] | None = None
    ) -> dict[str, Any]:
        return holo.screen_click(x, y, modifiers=modifiers)

    @mcp.tool(description="Type a literal string into whatever has keyboard focus.")
    def screen_type(text: str) -> dict[str, Any]:
        return holo.screen_type(text)

    @mcp.tool(
        description=(
            "Send a key combo (e.g. 'cmd+v', 'enter', 'shift+tab'). "
            "Sikuli's Key constants are recognised (ENTER, TAB, ESC, F1-F12, …)."
        )
    )
    def screen_key(combo: str) -> dict[str, Any]:
        return holo.screen_key(combo)

    @mcp.tool(
        description=(
            "Move to screen coordinates (x, y) and emit `steps` "
            "mouse-wheel events in `direction` ('up' or 'down', "
            "default 'down'). Use this when keyboard scroll won't "
            "work — e.g. a sidebar that doesn't have keyboard focus."
        )
    )
    def screen_scroll(
        x: int, y: int, direction: str = "down", steps: int = 3
    ) -> dict[str, Any]:
        return holo.screen_scroll(x, y, direction=direction, steps=steps)

    @mcp.tool(
        description=(
            "Move the cursor to (x, y) WITHOUT clicking, scrolling, or "
            "pressing any keys. Useful for triggering hover-only UI "
            "(tooltips, menus that open on mouseenter, reveal-on-hover "
            "toolbars) so you can take a screenshot and decide where to "
            "click next. Returns {moved: true, x, y}."
        )
    )
    def screen_move(x: int, y: int) -> dict[str, Any]:
        return holo.screen_move(x, y)

    @mcp.tool(
        description=(
            "Capture the screen (or a region) as a PNG. Returns "
            "{image: base64, format: 'png', byte_count}. Pass `region` "
            "as {x, y, width, height} to crop."
        )
    )
    def screen_shot(region: dict[str, int] | None = None) -> dict[str, Any]:
        return holo.screen_shot(region=region)

    @mcp.tool(
        description=(
            "Find a base64-encoded PNG `needle` on screen. Returns "
            "{x, y, width, height, score} or null if no match. "
            "`score` is the minimum similarity threshold (0..1, default 0.7)."
        )
    )
    def screen_find_image(
        needle: str,
        region: dict[str, int] | None = None,
        score: float = 0.7,
    ) -> dict[str, Any] | None:
        return holo.screen_find_image(needle, region=region, score=score)

    @mcp.tool(
        description=(
            "Run the interactive drag-rectangle capture and return the "
            "selected region's PNG. Returns `{image: base64, x, y, "
            "width, height}` on success or `{cancelled: true, reason}` "
            "if the user pressed Esc. Primarily exists so the "
            "split-machine `--remote-screen` topology can run the drag "
            "UI on a peer; local callers should usually use "
            "`ui_template_capture` instead, which also stores the "
            "result in the named-template cache."
        )
    )
    def screen_user_capture(
        prompt: str = "", timeout: float = 60.0
    ) -> dict[str, Any]:
        return holo.screen_user_capture(prompt=prompt, timeout=timeout)

    # ---- UI template cache --------------------------------------------------
    #
    # Persistent cache mapping (app, label) → stored PNG variants. Used to
    # turn natural-language UI references ("the kebab menu", "the bookmarks
    # bar work folder") into screen coordinates without redoing vision each
    # session. Templates are captured once (interactively or from a region
    # the agent already has) and reused.

    @mcp.tool(
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
        return holo.ui_template_capture(
            label,
            app=app,
            region=region,
            replace=replace,
            similarity=similarity,
            timeout=timeout,
            prompt=prompt,
        )

    @mcp.tool(
        description=(
            "List stored UI templates. `app=None` lists everything; pass an "
            "app name (or '_global') to filter."
        )
    )
    def ui_template_list(app: str | None = None) -> dict[str, Any]:
        return holo.ui_template_list(app=app)

    @mcp.tool(
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
        return holo.ui_template_find(label, app=app, region=region)

    @mcp.tool(
        description=(
            "Find a saved template and click its center. Raises if nothing "
            "matches — clicking the wrong place is worse than a clear error."
        )
    )
    def ui_template_click(
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return holo.ui_template_click(label, app=app, region=region)

    @mcp.tool(
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
        return holo.ui_template_delete(label, app=app, variant=variant)

    # ---- Chrome browser tools (AppleScript; macOS-only) ---------------------
    #
    # Prefer these over keystroke automation (`app_activate` + `screen_key`)
    # for any browser navigation — they're synchronous and don't fight
    # macOS focus.
    #
    # Gated by `no_browser=False` so input-only deployments (the
    # `holo mcp --screen --no-bookmarklet --no-browser --listen` peer
    # in the split-input-proxy topology) don't expose tools that the
    # agent on the other side never reaches anyway.

    if not no_browser:
        @mcp.tool(
            description=(
                "Set the URL of Chrome's active tab in the front window. "
                "Reliable navigation without keystroke simulation (macOS only)."
            )
        )
        def browser_navigate(url: str) -> dict[str, Any]:
            return holo.browser_navigate(url)

        @mcp.tool(
            description=(
                "Open a new tab in Chrome's front window. "
                "If `url` is omitted, the tab opens to the New Tab page."
            )
        )
        def browser_new_tab(url: str | None = None) -> dict[str, Any]:
            return holo.browser_new_tab(url)

        @mcp.tool(description="Close the active tab of Chrome's front window.")
        def browser_close_active_tab() -> dict[str, Any]:
            return holo.browser_close_active_tab()

        @mcp.tool(
            description=(
                "Make tab `index` (1-based) the active tab of Chrome's front "
                "window and bring Chrome to the foreground."
            )
        )
        def browser_activate_tab(index: int) -> dict[str, Any]:
            return holo.browser_activate_tab(index)

        @mcp.tool(
            description=(
                "List tabs in Chrome's front window. Returns "
                "{tabs: [{id, title, url, index}], active: index}."
            )
        )
        def browser_list_tabs() -> dict[str, Any]:
            return holo.browser_list_tabs()

        @mcp.tool(description="Read the URL of Chrome's active tab.")
        def browser_read_active_url() -> dict[str, Any]:
            return holo.browser_read_active_url()

        @mcp.tool(description="Read the title of Chrome's active tab.")
        def browser_read_active_title() -> dict[str, Any]:
            return holo.browser_read_active_title()

        @mcp.tool(description="Reload Chrome's active tab.")
        def browser_reload() -> dict[str, Any]:
            return holo.browser_reload()

        @mcp.tool(description="Navigate back in Chrome's active tab history.")
        def browser_back() -> dict[str, Any]:
            return holo.browser_back()

        @mcp.tool(description="Navigate forward in Chrome's active tab history.")
        def browser_forward() -> dict[str, Any]:
            return holo.browser_forward()

        @mcp.tool(
            description=(
                "Run a JS expression in Chrome's active tab via AppleScript "
                "and return its stringified result. Use this for arbitrary "
                "DOM queries (`document.querySelector('button')?.innerText`, "
                "`JSON.stringify(...)`, etc). "
                "Requires Chrome's 'Allow JavaScript from Apple Events' "
                "toggle (View → Developer); if disabled, the error message "
                "will say so — fall back to `bookmarklet_query` against a "
                "calibrated channel for CSP-safe DOM access."
            )
        )
        def browser_execute_js(js: str) -> dict[str, Any]:
            return holo.browser_execute_js(js)

    if not no_bookmarklet:
        @mcp.tool(
            description=(
                "DOM query through the bookmarklet channel — CSP-safe "
                "fallback for `browser_execute_js`. Reads `selector` and "
                "returns the named property (default 'innerText') or "
                "attribute. Pass `all=true` for a list of matches. "
                "Requires a calibrated `sid`."
            )
        )
        def bookmarklet_query(
            sid: str,
            selector: str,
            prop: str = "innerText",
            attr: str | None = None,
            all: bool = False,
            timeout: float = 5.0,
        ) -> dict[str, Any]:
            return holo.bookmarklet_query(
                sid, selector, prop=prop, attr=attr, all=all, timeout=timeout
            )

    # ---- LAN session discovery / capabilities (channel-independent) ---------
    #
    # Always exposed — these tools don't depend on a calibrated bookmarklet
    # channel or the JVM bridge. They're how an agent connected to one holo
    # instance learns about other holo hosts on the LAN and routes work to
    # them by hardware/software/package capability.

    @mcp.tool(
        description=(
            "Return live `holo mcp --announce` sessions on the local "
            "broadcast domain. Reads from a continuously-running mDNS "
            "cache, so the call is instant — no per-request browse "
            "delay. Each entry includes `instance`, `host`, `user`, "
            "`ips`, optional `session`/`ssh_user`/`tmux_*`, and (when "
            "the broadcaster used `--announce-capabilities`) "
            "`caps_port` + `caps_token`. Pass `instance` (or "
            "`session`/`host`) into `holo_fetch_capabilities` to read "
            "the host's hardware/software/package inventory. "
            "`wait_s` is an optional grace period — default 0; pass a "
            "small value (e.g. 1.5) only if you just told the user to "
            "start a new daemon and want it to land in the cache "
            "before the snapshot."
        )
    )
    def holo_discover_sessions(wait_s: float = 0.0) -> dict[str, Any]:
        return holo.holo_discover_sessions(wait_s=wait_s)

    @mcp.tool(
        description=(
            "Fetch a remote holo host's hardware / software / package "
            "inventory over the authenticated `/capabilities` HTTP endpoint. "
            "Reads the target session from the live discovery cache "
            "(no per-call mDNS browse), then HTTP-fetches with the "
            "broadcast token. `instance` is matched against the mDNS "
            "instance label, then `session`, then `host` — pass "
            "whichever the user gave you. Returns "
            "`{instance, host, session, ip_used, capabilities}` where "
            "`capabilities` follows the schema in docs/companion-spec.md "
            "§3a. Raises if the target session has no capabilities "
            "endpoint or if none of its advertised IPs is reachable. "
            "If the target isn't in the cache yet, call "
            "`holo_discover_sessions(wait_s=1.5)` first."
        )
    )
    def holo_fetch_capabilities(
        instance: str,
        timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        return holo.holo_fetch_capabilities(
            instance, timeout_s=timeout_s
        )

    @mcp.tool(
        description=(
            "Open a reverse SSH tunnel from this holo daemon to a "
            "CloudCity host on the LAN. The daemon picks the named "
            "CloudCity from its mDNS discovery cache, refreshes its "
            "SSH cert against the configured s3r9 backend, then runs "
            "`ssh -A -N -R 0:localhost:22 lando@<cc-ip>:<cc-port>`. "
            "Returns the allocated `tunnel_port`, plus the resolved "
            "`cloudcity_instance` / `cloudcity_host` / `cloudcity_target`. "
            "The desktop SPA's c2w VM should connect to "
            "`localhost:<tunnel_port>` from inside c2w-net to reach "
            "this daemon's tmux session — that's why the architecture "
            "exists. Idempotent at server scope: a second call replaces "
            "any existing tunnel cleanly. Pair with `holo_tunnel_down` "
            "for explicit teardown; the tunnel is also torn down on "
            "MCP server shutdown."
        )
    )
    def holo_tunnel_up(
        cloudcity_instance: str,
        backend: str | None = None,
        force_cert_refresh: bool = False,
    ) -> dict[str, Any]:
        return holo.holo_tunnel_up(
            cloudcity_instance,
            backend=backend,
            force_cert_refresh=force_cert_refresh,
        )

    @mcp.tool(
        description=(
            "Tear down the active reverse tunnel, if any. Idempotent — "
            "calling when no tunnel is up returns `{closed: false}`. "
            "Also clears the `tunnel_port` field from the daemon's "
            "`_holo-session._tcp.local.` mDNS announcement so "
            "discoverers see the routing change immediately."
        )
    )
    def holo_tunnel_down() -> dict[str, Any]:
        return holo.holo_tunnel_down()

    @mcp.tool(
        description=(
            "Launch the SikuliX IDE on the host running this holo "
            "daemon. Spawns `java -jar sikulixide-*.jar` as a child of "
            "the MCP server so macOS Accessibility / Screen-Recording "
            "permissions granted to the `holo` binary cover the IDE's "
            "mouse / keyboard simulation. Lazy-downloads the SikuliX "
            "jar on first call (same path as `holo install-screen`). "
            "Idempotent: a second call while the IDE is still running "
            "returns the existing PID with `already_running=true` "
            "instead of stacking JVMs. Returns "
            "`{pid, jar, java, already_running}`. Raises if `java` "
            "isn't on PATH or the jar can't be fetched. Use this when "
            "the user asks to design templates, capture screen "
            "regions interactively, or otherwise wants the SikuliX "
            "IDE up on their machine."
        )
    )
    def holo_launch_ide() -> dict[str, Any]:
        return holo.holo_launch_ide()

    # Phase 2 resource tools. Registered ONLY when at least one
    # resource is announced — without any resources the tools have
    # nothing to act on, so leaving them off the surface keeps clients
    # from discovering entry points that always error.
    if holo._resources:
        @mcp.tool(
            description=(
                "Return the full per-resource records declared on "
                "this daemon: name, path, tags, caps, "
                "allow_principals. Tai's `on` keyword uses this to "
                "resolve `{holo:tag=X}` selectors over the auto-"
                "tunnel; mirrors `GET /v1/resources` on the "
                "capabilities HTTP endpoint. allow_principals is "
                "informational only in v1 (see docs/resources.md)."
            )
        )
        def holo_list_resources() -> dict[str, Any]:
            return holo.list_resources()

        @mcp.tool(
            description=(
                "Run a shell body inside the scope of a declared "
                "resource on this daemon. The body must use only "
                "commands listed in the resource's caps=exec:... "
                "allowlist, and may not use absolute paths or '..'. "
                "cwd is pinned to the resource path; HOLO_HOST, "
                "HOLO_RESOURCE, HOLO_RESOURCE_PATH are injected into "
                "env. Output is batched into a frames list, each "
                "frame {fd: 'stdout'|'stderr', data: str}; the "
                "response includes exit, duration_ms, and timed_out."
            )
        )
        def holo_exec_in_resource(
            resource: str,
            body: str,
            env: dict[str, str] | None = None,
            timeout_s: int = 60,
        ) -> dict[str, Any]:
            return holo.exec_in_resource(
                resource, body, env=env, timeout_s=timeout_s
            )

    return mcp, holo


@contextmanager
def _sigterm_as_keyboard_interrupt() -> Iterator[None]:
    """Convert SIGTERM into KeyboardInterrupt while inside the block.

    Existing teardown paths already handle KeyboardInterrupt — they
    fall through to the `finally` clauses that call `holo.shutdown()`,
    which gives the announcer a chance to send mDNS Goodbye packets
    (TTL=0 records) before the process exits. Without this, `kill
    <pid>` leaves stale entries in every cache on the LAN until they
    age out (~75 s).

    The handler is restored on exit so test-suite re-entry doesn't
    poison subsequent runs. Only the main thread can call
    `signal.signal`; that's fine — `run`/`run_tcp` are CLI entrypoints.
    """

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise KeyboardInterrupt()

    try:
        previous = signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        # Not on the main thread (e.g. inside a test harness that
        # called this from a worker). Skip — graceful shutdown still
        # works for SIGINT via Python's default handler.
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run(
    *,
    hide_qr: bool = False,
    enable_screen: bool = False,
    no_bookmarklet: bool = False,
    no_browser: bool = False,
    input_proxy: tuple[str, int] | None = None,
    remote_screen: tuple[str, int] | None = None,
    announce: bool = False,
    announce_session: str | None = None,
    announce_user: str | None = None,
    announce_ssh_user: str | None = None,
    announce_ips: list[str] | None = None,
    announce_capabilities: bool = False,
    announce_command: str | None = None,
    announce_resources: list[Any] | None = None,
    auto_tunnel: bool = False,
    auto_tunnel_backend: str | None = None,
) -> None:
    """Entrypoint used by `holo mcp` — runs the server over stdio."""
    mcp, holo = build_server(
        hide_qr=hide_qr,
        enable_screen=enable_screen,
        no_bookmarklet=no_bookmarklet,
        no_browser=no_browser,
        input_proxy=input_proxy,
        remote_screen=remote_screen,
        announce=announce,
        announce_session=announce_session,
        announce_user=announce_user,
        announce_ssh_user=announce_ssh_user,
        announce_ips=announce_ips,
        announce_capabilities=announce_capabilities,
        announce_command=announce_command,
        announce_resources=announce_resources,
        auto_tunnel=auto_tunnel,
        auto_tunnel_backend=auto_tunnel_backend,
    )
    try:
        with _sigterm_as_keyboard_interrupt():
            mcp.run()
    except KeyboardInterrupt:
        # Normal shutdown path — fall through to finally for cleanup.
        pass
    finally:
        holo.shutdown()


def run_tcp(
    port: int,
    *,
    hide_qr: bool = False,
    enable_screen: bool = False,
    no_bookmarklet: bool = False,
    no_browser: bool = False,
    input_proxy: tuple[str, int] | None = None,
    remote_screen: tuple[str, int] | None = None,
    announce: bool = False,
    announce_session: str | None = None,
    announce_user: str | None = None,
    announce_ssh_user: str | None = None,
    announce_ips: list[str] | None = None,
    announce_capabilities: bool = False,
    announce_command: str | None = None,
    announce_resources: list[Any] | None = None,
    auto_tunnel: bool = False,
    auto_tunnel_backend: str | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Entrypoint used by `holo mcp --listen PORT`.

    Binds 127.0.0.1:PORT, accepts one client at a time. Each
    connection must send the magic prefix line before any MCP
    traffic; mismatched connections are dropped. Daemon state
    (calibrated channels, WS server, bridge) lives across
    connection lifetimes — clients can disconnect and reconnect
    without losing the registered tabs.

    `stop_event` is for tests — production callers leave it None
    and stop the loop with KeyboardInterrupt.
    """
    mcp, holo = build_server(
        hide_qr=hide_qr,
        enable_screen=enable_screen,
        no_bookmarklet=no_bookmarklet,
        no_browser=no_browser,
        input_proxy=input_proxy,
        remote_screen=remote_screen,
        announce=announce,
        announce_session=announce_session,
        announce_user=announce_user,
        announce_ssh_user=announce_ssh_user,
        announce_ips=announce_ips,
        announce_port=port,
        announce_capabilities=announce_capabilities,
        announce_command=announce_command,
        announce_resources=announce_resources,
        auto_tunnel=auto_tunnel,
        auto_tunnel_backend=auto_tunnel_backend,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError as e:
        print(f"holo mcp: bind 127.0.0.1:{port} failed: {e}", file=sys.stderr)
        listener.close()
        holo.shutdown()
        raise SystemExit(1) from e
    listener.listen(1)
    # Polling timeout so the accept loop notices stop_event.
    listener.settimeout(0.5)
    print(
        f"holo mcp: listening on 127.0.0.1:{port} (magic prefix required)",
        file=sys.stderr,
        flush=True,
    )

    try:
        with _sigterm_as_keyboard_interrupt():
            while stop_event is None or not stop_event.is_set():
                try:
                    conn, addr = listener.accept()
                except TimeoutError:
                    continue
                except KeyboardInterrupt:
                    break
                try:
                    handshake = read_handshake(conn)
                    if not is_valid_handshake(handshake):
                        print(
                            f"holo mcp: rejecting {addr}: "
                            f"bad handshake {handshake[:32]!r}",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    print(
                        f"holo mcp: accepted {addr}",
                        file=sys.stderr,
                        flush=True,
                    )
                    _serve_one_connection(mcp, conn)
                    print(
                        f"holo mcp: closed {addr}",
                        file=sys.stderr,
                        flush=True,
                    )
                except KeyboardInterrupt:
                    break
                finally:
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    conn.close()
    finally:
        listener.close()
        holo.shutdown()


def _serve_one_connection(mcp: FastMCP, conn: socket.socket) -> None:
    """Run the FastMCP server loop on a single TCP connection.

    Mirrors `FastMCP.run_stdio_async` but supplies socket-backed
    streams instead of sys.stdin/sys.stdout. Reaches for the
    underlying `_mcp_server` because FastMCP doesn't expose a
    stream-injecting public runner.
    """
    fin = conn.makefile("r", encoding="utf-8", errors="replace", newline="\n")
    fout = conn.makefile("w", encoding="utf-8", newline="\n")

    async def go() -> None:
        try:
            stdin = anyio.wrap_file(fin)
            stdout = anyio.wrap_file(fout)
            async with stdio_server(stdin=stdin, stdout=stdout) as (rs, ws):
                await mcp._mcp_server.run(  # noqa: SLF001 — no public stream-injecting runner
                    rs,
                    ws,
                    mcp._mcp_server.create_initialization_options(),  # noqa: SLF001
                )
        finally:
            try:
                fin.close()
            except OSError:
                pass
            try:
                fout.close()
            except OSError:
                pass

    try:
        anyio.run(go)
    except (anyio.EndOfStream, ConnectionResetError, BrokenPipeError):
        pass


# Re-export so callers can write the magic prefix without importing the
# wire helper module separately.
__all__ = [
    "HoloMCPServer",
    "build_server",
    "run",
    "run_tcp",
    "WIRE_MAGIC",
]
