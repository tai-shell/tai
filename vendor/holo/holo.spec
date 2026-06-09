# PyInstaller build spec for the holo daemon binary.
#
# Produces a single `dist/holo` executable that bundles:
#
#   * the Python interpreter + holo package + transitive deps
#   * src/holo/static/ (popup.html, framing.js — served by WSServer)
#   * bridge/bridge.py (Jython script invoked by `java -jar sikulix*.jar -r ...`)
#   * bookmarklet/dist/ (prebuilt JS + install.html — when present)
#
# What we do NOT bundle:
#
#   * sikulix*.jar (~128 MB) — fetched lazily by `BridgeClient` from a
#     pinned GitHub Release, or pre-warmed via `holo install-bridge`.
#   * OpenJDK — user installs separately.
#
# Build:    .venv/bin/pyinstaller --clean holo.spec
# Output:   dist/holo (macOS / Linux) or dist/holo.exe (Windows)
#
# The `Analysis` step's `pathex=['src']` lets PyInstaller find the
# `holo` package without a separate `pip install -e .` step in CI.

# ruff: noqa  (this file runs under PyInstaller's bundled exec context)

import os
from pathlib import Path

import PyInstaller.config

ROOT = Path(SPECPATH).resolve()

# When set, force a specific target arch for the EXE (e.g. universal2 on
# macOS). PyInstaller validates that the running Python supports the
# requested arch — set HOLO_TARGET_ARCH=universal2 in CI on macos-14
# (where we install python.org's universal2 build) to fail fast if a
# dependency isn't universal2 instead of silently producing arm64-only.
TARGET_ARCH = os.environ.get("HOLO_TARGET_ARCH") or None

datas = [
    (str(ROOT / "src" / "holo" / "static"), "holo/static"),
    (str(ROOT / "bridge" / "bridge.py"), "bridge"),
]

bookmarklet_dist = ROOT / "bookmarklet" / "dist"
if bookmarklet_dist.exists():
    datas.append((str(bookmarklet_dist), "bookmarklet/dist"))

# Hidden imports — modules PyInstaller's static analyser misses, mostly
# because they're imported lazily inside framework code.
hiddenimports = [
    # MCP SDK loads its transports / handlers via dynamic dispatch.
    "mcp.server.fastmcp",
    "mcp.server.lowlevel",
    "mcp.server.stdio",
    "mcp.shared.context",
    # FastMCP delegates to anyio + starlette under the hood.
    "anyio._backends._asyncio",
    "starlette.routing",
    "uvicorn.lifespan.on",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    # zeroconf (used by holo.announce) lazy-loads several internal
    # submodules; collect_submodules in a hook would be cleaner but
    # this enumeration is easy to keep current and avoids the hook
    # discovery path.
    "zeroconf",
    "zeroconf._handlers",
    "zeroconf._protocol",
    "zeroconf._services",
    "zeroconf._utils",
    "ifaddr",
]

a = Analysis(
    [str(ROOT / "src" / "holo" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # We ship our own Jython script via SikuliX; no use for Tk in the
        # daemon and it bloats the bundle by ~10 MB.
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="holo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)
