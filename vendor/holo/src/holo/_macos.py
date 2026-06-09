"""macOS-only AppKit / Quartz helpers (process activation, synthetic clicks).

`pyobjc-framework-Cocoa` (which provides AppKit / NSRunningApplication)
is a transitive dependency of `pyobjc-framework-Quartz`, so we don't
need to add it to pyproject.toml separately. Imports are local to
each call so this module can still be imported on non-darwin
platforms for tests / coverage.
"""

from __future__ import annotations

import subprocess
import sys as _sys


def activate_pid(pid: int) -> bool:
    """Bring the application with the given PID to the foreground.

    On macOS Sonoma (14)+ Apple restricted cross-app activation:
    `NSRunningApplication.activateWithOptions_` from a non-foreground
    process is silently denied. We try the modern API first (in case
    the calling app *is* the foreground app, or we're on an older
    OS), and fall back to AppleScript via osascript — which is
    treated by the OS as a privileged scripting framework and is
    still allowed to activate other apps.

    Returns True on success, False if no running app with that PID
    was found.
    """
    if pid <= 0:
        return False

    from AppKit import (  # type: ignore[import-not-found]
        NSApplicationActivateAllWindows,
        NSApplicationActivateIgnoringOtherApps,
        NSRunningApplication,
        NSWorkspace,
    )

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return False

    # Fast path: the modern API. May silently fail on Sonoma+ if we
    # don't have user activation, but it's the right thing to call
    # when it works.
    options = NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps
    app.activateWithOptions_(options)

    # Belt-and-suspenders: ask osascript to do the same. osascript is
    # one of the routes Apple still permits to activate other apps
    # without an existing user-activation token. We address by name
    # (the localized application name macOS exposes), not pid —
    # AppleScript can't take a pid.
    name = app.localizedName()
    if name:
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{name}" to activate'],
                check=False,
                capture_output=True,
                timeout=2.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # osascript should always be present on macOS, but if it
            # isn't, we've already made the AppKit call; nothing else
            # to do.
            pass

    # NSWorkspace's frontmostApplication is updated synchronously
    # after activation requests succeed. Returning the result of the
    # activation lets callers wait the right amount of time.
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    return front is not None and front.processIdentifier() == pid


def keystroke_paste(app_name: str | None = None) -> bool:
    """Send Cmd+V via osascript + System Events.

    pyautogui's `hotkey('command', 'v')` works for keystrokes destined
    for the terminal (proven in early demo runs), but the resulting
    paste event has been observed to never reach a Chrome popup's
    contenteditable. System Events / osascript keystrokes share the
    same Automation pipeline that we're already using for activation,
    and have been observed to land where the page's paste handler can
    see them.

    If `app_name` is given, the script activates that app first so
    the keystroke targets it. Otherwise the keystroke goes to the
    current frontmost app.

    Returns True on success, False if osascript fails or isn't
    available.
    """
    if app_name:
        script = (
            f'tell application "{app_name}" to activate\n'
            'delay 0.2\n'
            'tell application "System Events" to keystroke "v" using command down\n'
        )
    else:
        script = 'tell application "System Events" to keystroke "v" using command down\n'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5.0,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Stealth-mode QR colors. The bookmarklet renders dark modules as
# (124, 200, 120) and light modules as (120, 200, 120) — a 4-unit
# delta in the red channel only, which is below the threshold a human
# eye (or external phone camera with typical sensor noise) can pick
# out from a meter away. The daemon thresholds the captured pixels'
# red channel below before passing the bitmap to Vision.
STEALTH_PIVOT_R: int = 122  # midpoint between the two reds


def _amplify_stealth_qr(image_ref):
    """Return a CGImage where the bookmarklet's two near-identical
    greens have been mapped to black/white.

    Renders the captured image into a CGBitmapContext we own (forced
    to RGBA, device-RGB so values stay sRGB-numerically) and walks the
    buffer thresholding the red channel against `STEALTH_PIVOT_R`:
    above pivot → black (dark QR module), at-or-below → white. The
    popup body, title text, and textarea all sit far below pivot, so
    they collapse into the QR's quiet zone — Vision happily ignores
    extra white space around the symbol.

    Raw pixel iteration sidesteps Core Image's working-color-space
    quirks (CI filters operate in linear-light by default, so an
    sRGB-valued pivot produces wrong results without explicit color-
    space handling). One pass through ~230 k pixels is comfortably
    under our 150 ms poll cadence.
    """
    from Quartz import (  # type: ignore[import-not-found]
        CGBitmapContextCreate,
        CGBitmapContextCreateImage,
        CGColorSpaceCreateDeviceRGB,
        CGContextDrawImage,
        CGImageGetHeight,
        CGImageGetWidth,
        kCGImageAlphaPremultipliedLast,
    )

    width = CGImageGetWidth(image_ref)
    height = CGImageGetHeight(image_ref)
    if width == 0 or height == 0:
        return image_ref
    bytes_per_row = width * 4
    total = bytes_per_row * height

    # Allocate a Python-side buffer. CGBitmapContextCreate aliases
    # this buffer (it doesn't copy), so the bytes get written by
    # CGContextDrawImage and read by CGBitmapContextCreateImage.
    buf = bytearray(total)
    color_space = CGColorSpaceCreateDeviceRGB()
    ctx = CGBitmapContextCreate(
        buf, width, height, 8, bytes_per_row, color_space,
        kCGImageAlphaPremultipliedLast,
    )
    if ctx is None:
        return image_ref
    CGContextDrawImage(ctx, ((0, 0), (width, height)), image_ref)

    pivot = STEALTH_PIVOT_R

    # Optional one-shot histogram dump for debugging the threshold —
    # set HOLO_DEBUG_STEALTH=1 in the env to see what the captured
    # pixels look like before amplification.
    if _STEALTH_DEBUG:
        _dump_stealth_histogram(buf)

    # RGBA layout: byte 0 = R, byte 3 = A. Step 4 bytes per pixel.
    for i in range(0, total, 4):
        if buf[i] > pivot:
            buf[i] = 0
            buf[i + 1] = 0
            buf[i + 2] = 0
        else:
            buf[i] = 255
            buf[i + 1] = 255
            buf[i + 2] = 255

    out = CGBitmapContextCreateImage(ctx)
    return out if out is not None else image_ref


import os as _os  # noqa: E402

_STEALTH_DEBUG: bool = _os.environ.get("HOLO_DEBUG_STEALTH") == "1"


def _dump_stealth_histogram(buf: bytearray) -> None:
    """Print a one-shot R-channel histogram around the stealth pivot.

    Helps diagnose 'amplified QR doesn't decode' failures: we want to
    see two clear humps near 120 and 124 in the captured framebuffer.
    Disables itself after the first call so we don't spam the log.
    """
    global _STEALTH_DEBUG
    _STEALTH_DEBUG = False
    counts: dict[int, int] = {}
    for i in range(0, len(buf), 4):
        r = buf[i]
        counts[r] = counts.get(r, 0) + 1
    pivot = STEALTH_PIVOT_R
    near = sorted(
        (k, v) for k, v in counts.items() if pivot - 10 <= k <= pivot + 10
    )
    print(f"[holo stealth] R-channel histogram around pivot={pivot}:", file=_sys.stderr)
    for k, v in near:
        print(f"  R={k:3d}: {v}", file=_sys.stderr)
    above = sum(v for k, v in counts.items() if k > pivot)
    below = sum(v for k, v in counts.items() if k <= pivot)
    print(f"[holo stealth] above pivot: {above}, at-or-below: {below}", file=_sys.stderr)


def capture_window_qr(window_id: int, *, hide_qr: bool = False) -> str | None:
    """Capture the given window's pixels and decode any QR code present.

    The popup renders its replies as QR codes on a canvas because the
    title channel is OS-truncated for any payload longer than ~70
    characters. Pixel capture has no such limit and is CSP-immune,
    so it works as the universal page → daemon channel.

    When `hide_qr=True`, the popup paints the QR in two near-identical
    greens that humans / external cameras can't decode. We amplify the
    captured image's red-channel delta into a real black/white QR via
    a CIColorMatrix filter before handing it to Vision.

    Returns the QR's payload string, or None if:
    - the window can't be captured (closed, off-screen, no permission)
    - no QR is detected in the captured image
    - more than one QR is detected (we expect exactly one)

    Uses the macOS Vision framework, which ships with the OS — no
    third-party native dependency.
    """
    from Quartz import (  # type: ignore[import-not-found]
        CGWindowListCreateImage,
        kCGNullWindowID,  # noqa: F401  (kept for type-stub completeness)
        kCGWindowImageBoundsIgnoreFraming,
        kCGWindowImageDefault,
        kCGWindowListOptionIncludingWindow,
    )

    image_ref = CGWindowListCreateImage(
        ((0, 0), (0, 0)),  # CGRectNull — capture the whole window
        kCGWindowListOptionIncludingWindow,
        window_id,
        kCGWindowImageDefault | kCGWindowImageBoundsIgnoreFraming,
    )
    if image_ref is None:
        return None

    if hide_qr:
        image_ref = _amplify_stealth_qr(image_ref)

    from Vision import (  # type: ignore[import-not-found]
        VNDetectBarcodesRequest,
        VNImageRequestHandler,
    )

    request = VNDetectBarcodesRequest.alloc().init()
    request.setSymbologies_(["VNBarcodeSymbologyQR"])

    handler = VNImageRequestHandler.alloc().initWithCGImage_options_(image_ref, None)
    success, _err = handler.performRequests_error_([request], None)
    if not success:
        return None

    results = request.results() or []
    if not results:
        return None

    # We expect exactly one QR per popup at any given time. If the
    # popup is mid-render we may briefly see zero or two; caller
    # retries on a poll, so being strict here is safe.
    payloads = [obs.payloadStringValue() for obs in results]
    payloads = [p for p in payloads if p]
    if len(payloads) != 1:
        return None
    return str(payloads[0])


def click_at(x: float, y: float) -> None:
    """Synthesize a left-click at screen coordinates (x, y).

    Posts CGEvent mouse-down + mouse-up events directly to the HID tap
    so the visible mouse cursor doesn't move. Used to focus the
    popup's contenteditable body before sending Cmd+V — Chrome opens
    new popups with OS keyboard focus on the address bar, and JS
    `.focus()` cannot move OS focus out of browser chrome.

    Requires the same Accessibility permission as keystroke
    synthesis; without it the events are silently dropped.
    """
    from Quartz import (  # type: ignore[import-not-found]
        CGEventCreateMouseEvent,
        CGEventPost,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGHIDEventTap,
        kCGMouseButtonLeft,
    )

    point = (float(x), float(y))
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, point, kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, point, kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, up)
