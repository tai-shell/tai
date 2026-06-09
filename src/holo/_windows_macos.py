"""macOS implementation of holo.windows.

`pyobjc-framework-Quartz` is a darwin-only dependency (declared with a
`sys_platform == 'darwin'` marker in pyproject.toml). The Quartz import
is therefore inside `list_windows()` rather than at module load, so
this module can still be imported on non-Mac platforms for parsing-
only tests with a mocked CGWindowListCopyWindowInfo.
"""

from __future__ import annotations

from typing import Any

from holo.windows import WindowInfo


def list_windows() -> list[WindowInfo]:
    from Quartz import (  # type: ignore[import-not-found]
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly,
    )

    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    raw = CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []
    return [_parse(entry) for entry in raw if _is_visible(entry)]


def _is_visible(entry: dict[str, Any]) -> bool:
    """Filter out fully-transparent windows (alpha 0) — invisible to the user."""
    return entry.get("kCGWindowAlpha", 0) > 0


def _parse(entry: dict[str, Any]) -> WindowInfo:
    # `or` fallback covers both missing keys and keys present with None
    # (which CGWindowListCopyWindowInfo emits for windows whose title
    # is currently unreadable due to permissions).
    return WindowInfo(
        id=int(entry.get("kCGWindowNumber") or 0),
        title=str(entry.get("kCGWindowName") or ""),
        owner=str(entry.get("kCGWindowOwnerName") or ""),
        layer=int(entry.get("kCGWindowLayer") or 0),
        pid=int(entry.get("kCGWindowOwnerPID") or 0),
        bounds=_parse_bounds(entry.get("kCGWindowBounds")),
    )


def _parse_bounds(b: Any) -> tuple[float, float, float, float] | None:
    """Convert a kCGWindowBounds dict to an (x, y, w, h) tuple.

    CGWindowListCopyWindowInfo returns bounds as a CFDictionary with
    keys X, Y, Width, Height (numbers). We accept any duck-typed
    mapping with those keys so tests can pass plain dicts.
    """
    if b is None:
        return None
    try:
        return (float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]))
    except (KeyError, TypeError, ValueError):
        return None
