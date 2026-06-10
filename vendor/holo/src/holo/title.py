"""Python mirror of bookmarklet/title.js — parses holo title markers.

The bookmarklet writes responses and beacons into `document.title`; the
daemon reads them via `holo.windows.list_windows()`. Two title formats:

    `<originalTitle> [holo:1:<base64-frame-json>]`   framed (full frame)
    `<originalTitle> [holo:<marker>]`                plain (status beacon)

Markers always live at the end of the title.

This module is deliberately small and standalone. The wire format must
stay byte-for-byte interoperable with bookmarklet/title.js.
"""

from __future__ import annotations

import base64
import binascii
import re

# Browsers append "- Google Chrome" / "— Firefox" / etc. to the OS-level
# window title, so our marker (which the bookmarklet writes at the end of
# document.title) ends up in the middle of the OS title we read via Quartz.
# Match the marker anywhere; only one is present per title in practice.
_FRAMED_RE = re.compile(r"\[holo:1:([A-Za-z0-9+/=]+)\]")
_PLAIN_RE = re.compile(r"\[holo:([^\]]+)\]")


def decode_framed(title: str) -> str | None:
    """Return the JSON frame string from `[holo:1:<base64>]`, or None."""
    if not isinstance(title, str):
        return None
    m = _FRAMED_RE.search(title)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1), validate=True).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def decode_plain(title: str) -> str | None:
    """Return the marker string from `[holo:<marker>]` (non-framed), or None.

    The framed `[holo:1:...]` form is intentionally rejected here so a
    framed payload can't be misread as a plain marker.
    """
    if not isinstance(title, str):
        return None
    m = _PLAIN_RE.search(title)
    if not m:
        return None
    payload = m.group(1)
    if payload.startswith("1:"):
        return None
    return payload


def is_holo_title(title: str) -> bool:
    return decode_framed(title) is not None or decode_plain(title) is not None
