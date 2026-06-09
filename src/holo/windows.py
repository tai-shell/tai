"""Window enumeration and title reading.

This is the page → daemon side of the channel: the daemon polls window
titles to receive replies and beacons that the bookmarklet writes via
`document.title`. Browsers reflect the page title into the OS window
title (typically with a browser-name suffix), so reading the OS-level
title is enough — no extension or DevTools protocol required.

Currently macOS-only. The implementation uses Quartz CoreGraphics'
`CGWindowListCopyWindowInfo`. On macOS Sonoma (14) and later, reading
window titles for windows the calling process does not own requires
Screen Recording permission. Without that permission the API still
returns entries, but the `kCGWindowName` field is empty. The daemon's
permissions doctor (later) will check for and prompt for this grant.

Linux and Windows implementations will follow; calls on those
platforms currently raise NotImplementedError.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class WindowInfo:
    id: int
    title: str
    owner: str
    layer: int
    pid: int = 0
    # On-screen bounds in screen coordinates: (x, y, width, height).
    # macOS's coordinate space has the origin at the top-left of the
    # primary display, growing right and down — same convention as
    # Quartz event coordinates and pyautogui.
    bounds: tuple[float, float, float, float] | None = None


def list_windows() -> list[WindowInfo]:
    """Return information for every visible on-screen window.

    Empty `title` means either the window has no title set or the OS
    refused to disclose it (Screen Recording permission missing on
    macOS 14+). Callers should treat empty titles as "unreadable",
    not as "untitled."
    """
    if sys.platform == "darwin":
        from holo._windows_macos import list_windows as _impl

        return _impl()
    raise NotImplementedError(f"holo.windows is not yet implemented on {sys.platform}")
