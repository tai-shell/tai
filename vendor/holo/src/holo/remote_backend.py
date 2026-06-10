"""Proxy input and capture ops to a remote `holo mcp` over the wire.

Use cases:

  - **Input proxy** (`holo mcp --input-proxy / --remote-input HOST:PORT`):
    machine A captures locally but corporate policy blocks event injection;
    a peer (machine B) with Screen Sharing into A injects events that A's
    Screen Sharing service relays back to A's display.
  - **Screen proxy** (`holo mcp --remote-screen HOST:PORT`): the inverse —
    machine A can't do screen capture (Claude Code as the responsible
    process lacks Screen Recording TCC), so reads of A's screen are
    routed to a peer that captures *its own* display (which, in the
    Screen-Sharing-of-A topology, IS A's display content).
  - Both flags together → A becomes a pure orchestrator: every screen op
    runs on B. When both proxies target the same host, holo shares one
    MCP connection.

This module owns the A→remote side of that channel:

  - Spawns ``holo connect HOST:PORT`` as a child subprocess (existing
    stdio↔TCP bridge with magic-prefix handshake, single connection).
  - Wraps the subprocess's stdio in an MCP ``ClientSession`` from the
    official `mcp` SDK.
  - Exposes a sync facade whose method signatures and return shapes
    match `BridgeClient` so the Daemon can pick
    `daemon._remote_input or daemon.bridge` (and likewise for
    `_remote_screen`) without any other branching.

The MCP session runs inside a dedicated asyncio loop on a background
thread; sync methods submit coroutines with
``asyncio.run_coroutine_threadsafe`` and block on the result. The
session and the subprocess stay open for the daemon's lifetime —
each op is a fresh ``call_tool``, not a fresh connection.

macOS-first.
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class RemoteHoloError(RuntimeError):
    """Raised when the remote channel can't be established or a proxied
    tool call fails on the remote side."""


# How long the synchronous facade waits for the async loop to come up
# and for each individual tool call. Tool calls are inherently
# interactive (mouse moves / screen captures on a remote machine), so
# the per-call ceiling is generous; the startup ceiling is tighter
# because a hung connect would otherwise wedge the daemon at boot.
STARTUP_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 30.0


def _resolve_holo_exe() -> str:
    """Find the `holo` binary to invoke for the connect subprocess.

    Order:
    1. ``HOLO_EXE`` env var — explicit override for tests / unusual setups.
    2. PyInstaller frozen build → ``sys.executable`` IS the holo binary.
    3. Dev install → ``shutil.which("holo")``.
    4. Last resort: ``sys.executable`` (may or may not be holo).
    """
    env = os.environ.get("HOLO_EXE")
    if env:
        return env
    if getattr(sys, "frozen", False):
        return sys.executable
    found = shutil.which("holo")
    if found:
        return found
    return sys.executable


class RemoteHoloBackend:
    """Sync facade over an MCP client connected to a remote holo daemon.

    Method signatures and return shapes match `BridgeClient` so the
    Daemon can pick `self._remote_input or self.bridge` (and likewise
    for `self._remote_screen`) without any other branching.

    One instance per remote endpoint. Holds one connect subprocess +
    one MCP session for its lifetime; both input and capture methods
    can be called on the same instance (the daemon shares one backend
    when `--input-proxy` and `--remote-screen` point at the same host).
    """

    def __init__(self, host: str, port: int, *, holo_exe: str | None = None) -> None:
        self.host = host
        self.port = port
        self._holo_exe = holo_exe or _resolve_holo_exe()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None

        # Async context managers we entered manually so we can exit
        # them at shutdown. `stdio_client` returns a (read, write)
        # tuple stream pair; `ClientSession` wraps it.
        self._stdio_cm: Any = None
        self._session_cm: Any = None

        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopped = False

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Spawn the connect subprocess and open an MCP session.

        Blocks until the session is initialized or fails. Raises
        `RemoteHoloError` on any startup failure (connect spawn
        failure, magic-prefix rejection by remote, MCP init timeout).
        Idempotent: a second `start()` on an already-running backend
        is a no-op.
        """
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"holo-remote-{self.host}-{self.port}",
            daemon=True,
        )
        self._thread.start()
        ok = self._ready.wait(timeout=STARTUP_TIMEOUT_S)
        if not ok:
            raise RemoteHoloError(
                f"timed out connecting to {self.host}:{self.port} after "
                f"{STARTUP_TIMEOUT_S}s — is `holo mcp --listen {self.port}` "
                "running on the remote?"
            )
        if self._startup_error is not None:
            raise self._startup_error

    def stop(self) -> None:
        """Tear down the MCP session and the connect subprocess.

        Idempotent. Safe to call from arbitrary threads; the loop
        runs the cleanup coroutine and then stops itself.
        """
        if self._stopped or self._loop is None:
            self._stopped = True
            return
        self._stopped = True
        loop = self._loop
        try:
            fut = asyncio.run_coroutine_threadsafe(self._close_session(), loop)
            try:
                fut.result(timeout=5.0)
            except Exception:
                pass
        finally:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
            if self._thread is not None:
                self._thread.join(timeout=5.0)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._open_session())
            if self._startup_error is None:
                loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _open_session(self) -> None:
        try:
            params = StdioServerParameters(
                command=self._holo_exe,
                args=["connect", f"{self.host}:{self.port}"],
                env=os.environ.copy(),
            )
            self._stdio_cm = stdio_client(params)
            read, write = await self._stdio_cm.__aenter__()
            self._session_cm = ClientSession(read, write)
            self._session = await self._session_cm.__aenter__()
            await self._session.initialize()
        except Exception as e:  # noqa: BLE001 — surface every startup failure as one error
            self._startup_error = RemoteHoloError(
                f"failed to open MCP session to {self.host}:{self.port}: {e}"
            )
        finally:
            self._ready.set()

    async def _close_session(self) -> None:
        # Order matters: exit ClientSession first (stops its
        # background readers), then the stdio_client (which waits for
        # the child subprocess to drain and exit).
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._session_cm = None
            self._session = None
        if self._stdio_cm is not None:
            try:
                await self._stdio_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._stdio_cm = None

    # ---- call ------------------------------------------------------------

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Run a remote tool and return whatever the tool returned.

        Returns a dict when the tool's return type is dict-shaped
        (FastMCP populates ``structuredContent``), parses JSON from
        the first text content block if structured content is absent,
        falls back to the raw text otherwise, and returns ``None``
        when the tool returned None (no structured content, no text).

        Tool errors (``isError=True`` on the result) raise
        `RemoteHoloError` with the remote's error text.
        """
        if self._stopped:
            raise RemoteHoloError("backend already stopped")
        if self._session is None or self._loop is None:
            raise RemoteHoloError("backend not started — call start() first")

        coro = self._session.call_tool(tool, arguments)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            result = fut.result(timeout=CALL_TIMEOUT_S)
        except TimeoutError as e:
            raise RemoteHoloError(
                f"remote tool {tool!r} timed out after {CALL_TIMEOUT_S}s"
            ) from e
        except Exception as e:
            raise RemoteHoloError(f"remote tool {tool!r} raised: {e}") from e

        if getattr(result, "isError", False):
            # FastMCP serializes raised exceptions as a TextContent block
            # with the error message. Surface it verbatim — the agent /
            # caller is the right place to decide retry semantics.
            msg = _first_text(result.content) or f"remote {tool!r} reported error"
            raise RemoteHoloError(f"remote tool {tool!r}: {msg}")

        # FastMCP returns dict-shaped tool results in `structuredContent`.
        sc = getattr(result, "structuredContent", None)
        if isinstance(sc, dict):
            return sc

        # Fallback: parse the first text block as JSON, else return raw.
        text = _first_text(result.content)
        if text:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return {"text": text}

        # Truly empty result: tool returned None or content blocks were
        # all non-text. Surface None to the caller — find_image relies on
        # this to signal "no match".
        return None

    # ---- input surface (matches BridgeClient input methods) -------------

    def click(
        self, x: int, y: int, *, modifiers: list[str] | None = None
    ) -> dict[str, Any]:
        return self._call(
            "screen_click",
            {"x": x, "y": y, "modifiers": modifiers or []},
        )

    def key(self, combo: str) -> dict[str, Any]:
        return self._call("screen_key", {"combo": combo})

    def type_text(self, text: str) -> dict[str, Any]:
        return self._call("screen_type", {"text": text})

    def scroll(
        self,
        x: int,
        y: int,
        *,
        direction: str = "down",
        steps: int = 3,
    ) -> dict[str, Any]:
        return self._call(
            "screen_scroll",
            {"x": x, "y": y, "direction": direction, "steps": steps},
        )

    def mouse_move(self, x: int, y: int) -> dict[str, Any]:
        return self._call("screen_move", {"x": x, "y": y})

    def activate(self, name: str) -> dict[str, Any]:
        return self._call("app_activate", {"name": name})

    # ---- capture surface (matches BridgeClient capture methods) ---------
    #
    # The remote tools return base64-encoded PNGs over the wire because
    # MCP / JSON-RPC can't carry raw bytes. We encode/decode here so the
    # facade keeps BridgeClient's bytes-in / bytes-out signatures.

    def screenshot(
        self, *, region: dict[str, int] | None = None, timeout: float = 15.0
    ) -> bytes:
        del timeout  # remote tool has its own per-call timeout
        result = self._call("screen_shot", {"region": region})
        if not isinstance(result, dict) or "image" not in result:
            raise RemoteHoloError(
                f"remote screen_shot returned unexpected shape: {result!r}"
            )
        return _b64.b64decode(result["image"])

    def find_image(
        self,
        needle: bytes,
        *,
        region: dict[str, int] | None = None,
        score: float = 0.7,
        timeout: float = 15.0,
    ) -> dict[str, Any] | None:
        del timeout
        needle_b64 = _b64.b64encode(needle).decode("ascii")
        result = self._call(
            "screen_find_image",
            {"needle": needle_b64, "region": region, "score": score},
        )
        if isinstance(result, dict) and result:
            return result
        # find returned None (no match) or empty — same semantics as
        # BridgeClient.find_image.
        return None

    def find_image_path(
        self,
        path: str | Path,
        *,
        region: dict[str, int] | None = None,
        score: float = 0.7,
        timeout: float = 15.0,
    ) -> dict[str, Any] | None:
        """Load `path` from the LOCAL filesystem and proxy as `find_image`.

        The remote's filesystem doesn't have our local template cache,
        so we can't just send the path. Read the bytes here on the
        Daemon's side and let the remote do the matching against its
        own screen.
        """
        try:
            needle = Path(path).read_bytes()
        except OSError as e:
            raise RemoteHoloError(
                f"could not read template at {path!r}: {e}"
            ) from e
        return self.find_image(needle, region=region, score=score, timeout=timeout)

    def user_capture(
        self, *, prompt: str = "", timeout: float = 60.0
    ) -> dict[str, Any]:
        """Run the interactive drag-rectangle capture on the remote.

        Returns the same dict shape as BridgeClient.user_capture —
        on success, ``{"image": base64_png, "x", "y", "width",
        "height"}``; on cancel, ``{"cancelled": True, "reason": ...}``.

        The drag overlay renders on the REMOTE machine's display. In the
        intended topology (where the remote has Screen Sharing into the
        local machine), the overlay appears on the local user's view of
        the shared screen — the user drags as if it were local.
        """
        return self._call(
            "screen_user_capture",
            {"prompt": prompt, "timeout": timeout},
        )


def _first_text(content: Any) -> str | None:
    """Extract the first text payload from an MCP CallToolResult.content list."""
    if not content:
        return None
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return None
