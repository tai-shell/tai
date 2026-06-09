"""Clipboard primitives for the daemon → page channel.

Per the architecture brief, clipboard-paste is the universal failsafe
for delivering commands to the in-page bookmarklet. It works on every
origin (no CSP involvement — clipboard contents are not subject to
content-security-policy), it handles arbitrary characters (no
keyboard-layout or escaping bugs), and it's bounded-disturbance
(write → paste → restore happens in ~100 ms).

The transmit pattern:

    saved = clipboard.read()
    clipboard.paste(framed_command)   # writes, sends Cmd/Ctrl+V, restores

The bookmarklet listens for `paste` events on a hidden contenteditable
that the daemon focuses (via OS-layer click on a templated coordinate)
before invoking `paste()`. Receipt is acknowledged out-of-band via
`document.title`, which `holo.windows.list_windows()` reads.

`paste()` does not wait for an ack itself — that's the channel layer's
job. This module is the OS-level primitive only.
"""

from __future__ import annotations

import sys
import time

DEFAULT_SETTLE_SECONDS = 0.05
DEFAULT_PASTE_SECONDS = 0.10


def read() -> str:
    """Return the current clipboard text. Empty string if the clipboard is empty."""
    import pyperclip

    return pyperclip.paste()


def write(text: str) -> None:
    """Replace clipboard contents with `text`."""
    import pyperclip

    pyperclip.copy(text)


def paste(
    text: str,
    *,
    restore: bool = True,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    paste_seconds: float = DEFAULT_PASTE_SECONDS,
) -> None:
    """Write `text`, send Cmd/Ctrl+V, optionally restore prior clipboard.

    The currently focused element (managed by the caller — typically a
    hidden contenteditable on the bookmarklet side) receives the paste.
    Caller is responsible for ensuring focus is on the right target;
    this module makes no assumption about which window is active.

    If `restore` is True (default), the prior clipboard contents are
    captured before the write and re-copied after `paste_seconds`.
    Total user-visible disturbance is `settle_seconds + paste_seconds`,
    typically ~150 ms.

    `settle_seconds` exists because some OSes need a moment for the
    new clipboard contents to be available to the paste consumer
    after the copy call. `paste_seconds` is the window between
    sending the keystroke and restoring; too short and the page
    receives the restored contents instead of `text`.
    """
    import pyautogui
    import pyperclip

    saved = pyperclip.paste() if restore else None

    pyperclip.copy(text)
    if settle_seconds > 0:
        time.sleep(settle_seconds)

    modifier = "command" if sys.platform == "darwin" else "ctrl"
    pyautogui.hotkey(modifier, "v")

    if restore:
        if paste_seconds > 0:
            time.sleep(paste_seconds)
        pyperclip.copy(saved)
