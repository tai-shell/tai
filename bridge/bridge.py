# Jython bridge for holo.
#
# Runs inside SikuliX's bundled Jython 2.7 via:
#
#     java -jar sikulixapi.jar -r bridge.py -- [bridge args]
#
# Bridge args (after the `--` SikuliX uses to separate its own args from
# script args):
#
#     --transport stdio                       (default; daemon spawns JVM as child)
#     --transport tcp --bind 127.0.0.1 --port 7081 [--token T]
#
# Reads line-delimited JSON-RPC requests from the chosen transport,
# dispatches them to SikuliX APIs, writes JSON-RPC responses back. The
# protocol is transport-agnostic: dispatch() takes a parsed request dict
# and returns a parsed response dict; transports just frame and unframe.
#
# Important: this file runs under Jython 2.7. No f-strings, no type
# hints, no `from __future__ import annotations`, no walrus operator.
# Keep it boring.

import json
import sys
import traceback

# SikuliX classes & helpers. Guarded so the file can be syntax-checked
# by Python 3 tools without SikuliX on the classpath.
try:
    import sikuli  # noqa: F401  (provides App, Key, KeyModifier, Location, click, type, ...)
    from sikuli import App, Key, KeyModifier, Location
    _SIKULI_OK = True
except ImportError:
    sikuli = None
    App = Key = KeyModifier = Location = None
    _SIKULI_OK = False


# SikuliX writes action logs ("[log] click on (x,y)", "[log] doType ...")
# to stdout by default. Stdout is our JSON-RPC channel — any extra line
# corrupts the protocol, since the daemon does a blocking readline()
# expecting one envelope per request. Silence every logging surface we
# know about. Settings.* are public; Debug.* covers the older paths.
if _SIKULI_OK:
    try:
        from org.sikuli.basics import Debug, Settings
        Debug.off()
        Settings.ActionLogs = False
        Settings.InfoLogs = False
        Settings.DebugLogs = False
    except ImportError:
        pass


PROTOCOL_VERSION = "1"


# ---- handlers -----------------------------------------------------------
#
# Each handler takes a dict of params and returns a JSON-serialisable
# result dict. Errors are raised as ordinary exceptions; dispatch()
# converts them into JSON-RPC error envelopes.

def handle_ping(_params):
    return {
        "pong": True,
        "protocol": PROTOCOL_VERSION,
        "sikuli": _SIKULI_OK,
    }


def handle_app_activate(params):
    name = params["name"]
    app = App(name)
    app.focus()
    return {"focused": True, "name": name}


def handle_screen_click(params):
    x = int(params["x"])
    y = int(params["y"])
    modifiers = params.get("modifiers", []) or []
    location = Location(x, y)
    if modifiers:
        for m in modifiers:
            sikuli.keyDown(_resolve_modifier_key(m))
        try:
            sikuli.click(location)
        finally:
            for m in modifiers:
                sikuli.keyUp(_resolve_modifier_key(m))
    else:
        sikuli.click(location)
    return {"clicked": True, "x": x, "y": y}


def handle_screen_key(params):
    # combo: "cmd+v", "shift+enter", "tab"
    combo = params["combo"]
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    target = _resolve_key(parts[-1])
    modifiers = parts[:-1]
    if modifiers:
        mask = 0
        for m in modifiers:
            mask = mask | _resolve_modifier_mask(m)
        sikuli.type(target, mask)
    else:
        sikuli.type(target)
    return {"sent": combo}


def handle_screen_type(params):
    text = params["text"]
    sikuli.type(text)
    return {"typed_chars": len(text)}


def handle_screen_shot(params):
    # Capture either a region {x, y, width, height} or the full primary screen.
    region = params.get("region")
    screen = sikuli.Screen()
    if region:
        capture = screen.capture(
            int(region["x"]),
            int(region["y"]),
            int(region["width"]),
            int(region["height"]),
        )
    else:
        capture = screen.capture()
    return {"image": _encode_png_b64(capture.getImage())}


def handle_screen_find_image(params):
    # Needle is a base64-encoded PNG. Score threshold (0..1) bounds the
    # match's similarity; lower means we accept fuzzier matches.
    import base64

    needle_b64 = params["needle"]
    score = float(params.get("score", 0.7))
    needle_bytes = base64.b64decode(needle_b64)
    needle_path = _write_temp_png(needle_bytes)
    try:
        target = sikuli.Screen()
        if "region" in params and params["region"] is not None:
            r = params["region"]
            target = sikuli.Region(
                int(r["x"]),
                int(r["y"]),
                int(r["width"]),
                int(r["height"]),
            )
        try:
            pattern = sikuli.Pattern(needle_path).similar(score)
            match = target.exists(pattern, 0)
        except Exception:
            match = None
        if match is None:
            return None
        return {
            "x": int(match.getX()),
            "y": int(match.getY()),
            "width": int(match.getW()),
            "height": int(match.getH()),
            "score": float(match.getScore()),
        }
    finally:
        _remove_quietly(needle_path)


def handle_screen_find_image_path(params):
    # Same as screen.find_image, but the needle is a path on the JVM-side
    # filesystem instead of a base64-encoded blob. Used by the template
    # cache, which already has the PNG on disk under <root>/<app>/<name>.png
    # and would just be re-encoding/decoding bytes for no gain.
    path = params["path"]
    score = float(params.get("score", 0.7))
    target = sikuli.Screen()
    if "region" in params and params["region"] is not None:
        r = params["region"]
        target = sikuli.Region(
            int(r["x"]),
            int(r["y"]),
            int(r["width"]),
            int(r["height"]),
        )
    try:
        pattern = sikuli.Pattern(path).similar(score)
        match = target.exists(pattern, 0)
    except Exception:
        match = None
    if match is None:
        return None
    return {
        "x": int(match.getX()),
        "y": int(match.getY()),
        "width": int(match.getW()),
        "height": int(match.getH()),
        "score": float(match.getScore()),
    }


def handle_screen_user_capture(params):
    # Blocks until the user finishes a rectangle selection (or cancels
    # with Esc). Returns the captured image as base64 PNG plus the
    # screen-coordinate rect. {"cancelled": true} on Esc/timeout — the
    # agent surface re-prompts rather than treating it as an error.
    timeout = float(params.get("timeout", 60.0))
    prompt = params.get("prompt", "")
    region = sikuli.Screen()
    # SikuliX's userCapture takes a prompt string and returns a
    # ScreenImage (or None on cancel). We pass `timeout` via Settings
    # if available; otherwise the caller's transport timeout governs.
    try:
        if prompt:
            captured = region.userCapture(prompt)
        else:
            captured = region.userCapture()
    except Exception as e:
        # Some Sikuli builds throw on cancel rather than returning None.
        # Mirror cancel semantics so the daemon doesn't see a hard error.
        return {"cancelled": True, "reason": str(e)}
    if captured is None:
        return {"cancelled": True, "reason": "user cancelled"}
    # ScreenImage exposes .getROI() (java.awt.Rectangle) and .getImage().
    roi = captured.getROI()
    image = captured.getImage()
    return {
        "image": _encode_png_b64(image),
        "x": int(roi.x),
        "y": int(roi.y),
        "width": int(roi.width),
        "height": int(roi.height),
    }


def handle_screen_scroll(params):
    # Move the mouse to (x, y) and emit `steps` wheel events in the
    # given direction. SikuliX's module-level `wheel(target, dir, steps)`
    # handles the move-and-emit in one call. Direction maps: "down" = 1,
    # "up" = -1 (matches SikuliX's WHEEL_DOWN / WHEEL_UP constants).
    x = int(params["x"])
    y = int(params["y"])
    direction = params.get("direction", "down")
    steps = int(params.get("steps", 3))
    if direction == "down":
        d = 1
    elif direction == "up":
        d = -1
    else:
        raise ValueError("direction must be 'up' or 'down'")
    sikuli.wheel(Location(x, y), d, steps)
    return {
        "scrolled": True,
        "x": x,
        "y": y,
        "direction": direction,
        "steps": steps,
    }


def handle_screen_move(params):
    # Move the cursor to (x, y) without clicking, scrolling, or
    # pressing any modifiers. Useful for triggering hover-only UI
    # (tooltips, menu reveals, dropdowns that open on mouseenter)
    # before deciding where to click. SikuliX's module-level
    # `hover(target)` moves the cursor and waits the configured
    # MoveMouseDelay (default 0.5s); we drop that delay to zero for
    # one call so the move is effectively instant.
    x = int(params["x"])
    y = int(params["y"])
    location = Location(x, y)
    # Settings.MoveMouseDelay is the post-move pause. Save and restore
    # so we don't change global SikuliX behavior for other ops in the
    # same bridge process.
    try:
        from org.sikuli.basics import Settings
        prev = Settings.MoveMouseDelay
        Settings.MoveMouseDelay = 0
        try:
            sikuli.hover(location)
        finally:
            Settings.MoveMouseDelay = prev
    except ImportError:
        # SikuliX not on classpath (impossible in production; defensive
        # for the syntax-check-with-CPython case). Just hover.
        sikuli.hover(location)
    return {"moved": True, "x": x, "y": y}


HANDLERS = {
    "ping": handle_ping,
    "app.activate": handle_app_activate,
    "screen.click": handle_screen_click,
    "screen.key": handle_screen_key,
    "screen.type": handle_screen_type,
    "screen.shot": handle_screen_shot,
    "screen.find_image": handle_screen_find_image,
    "screen.find_image_path": handle_screen_find_image_path,
    "screen.user_capture": handle_screen_user_capture,
    "screen.scroll": handle_screen_scroll,
    "screen.move": handle_screen_move,
}


# ---- image / file helpers ----------------------------------------------

def _encode_png_b64(buffered_image):
    # Encodes an `java.awt.image.BufferedImage` to a base64 PNG string.
    import base64

    from java.io import ByteArrayOutputStream
    from javax.imageio import ImageIO

    buf = ByteArrayOutputStream()
    ImageIO.write(buffered_image, "PNG", buf)
    return base64.b64encode(buf.toByteArray().tostring()).decode("ascii")


def _write_temp_png(data):
    # Write bytes to a tempfile and return its path. SikuliX matchers
    # take filesystem paths or Pattern objects; ImageIO can decode
    # in-memory but Pattern() is the smoothest entry point so we hit
    # the disk briefly.
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".png", prefix="holo-needle-")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return path


def _remove_quietly(path):
    import os

    try:
        os.unlink(path)
    except OSError:
        pass


# ---- key/modifier resolution -------------------------------------------

def _resolve_key(name):
    # Sikuli's Key class has constants like Key.ENTER, Key.TAB, Key.F1.
    # For any name we don't recognise as a constant, return the raw string
    # so type("a") sends a literal character.
    upper = name.upper()
    if Key is not None and hasattr(Key, upper):
        return getattr(Key, upper)
    return name


def _resolve_modifier_key(name):
    # For keyDown/keyUp we need the Key constant, not the modifier mask.
    upper = name.upper()
    aliases = {"CMD": "META", "COMMAND": "META", "WIN": "META", "OPT": "ALT"}
    upper = aliases.get(upper, upper)
    return getattr(Key, upper)


def _resolve_modifier_mask(name):
    upper = name.upper()
    aliases = {"CMD": "META", "COMMAND": "META", "WIN": "META", "OPT": "ALT"}
    upper = aliases.get(upper, upper)
    return getattr(KeyModifier, upper)


# ---- dispatch ----------------------------------------------------------

def dispatch(request):
    rid = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    handler = HANDLERS.get(method)
    if handler is None:
        return {
            "id": rid,
            "error": {"code": -32601, "message": "method not found: " + str(method)},
        }
    try:
        result = handler(params)
        return {"id": rid, "result": result}
    except Exception as e:
        return {
            "id": rid,
            "error": {
                "code": -32603,
                "message": str(e),
                "trace": traceback.format_exc(),
            },
        }


def _process_line(line):
    line = line.strip()
    if not line:
        return None
    try:
        request = json.loads(line)
    except ValueError as e:
        return {
            "id": None,
            "error": {"code": -32700, "message": "parse error: " + str(e)},
        }
    return dispatch(request)


# ---- transports --------------------------------------------------------

def serve_stdio():
    while True:
        line = sys.stdin.readline()
        if not line:
            return  # EOF — daemon closed our stdin
        response = _process_line(line)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


def serve_tcp(host, port, token):
    # Java's networking from Jython — keeps us off the OS-Python socket
    # module and consistent across platforms.
    from java.net import InetSocketAddress, ServerSocket

    server = ServerSocket()
    server.bind(InetSocketAddress(host, port))
    sys.stderr.write("[bridge] tcp listening on {0}:{1}\n".format(host, port))
    sys.stderr.flush()
    try:
        while True:
            client = server.accept()
            try:
                _serve_tcp_connection(client, token)
            finally:
                client.close()
    finally:
        server.close()


def _serve_tcp_connection(client, token):
    from java.io import (
        BufferedReader,
        BufferedWriter,
        InputStreamReader,
        OutputStreamWriter,
    )

    reader = BufferedReader(InputStreamReader(client.getInputStream(), "UTF-8"))
    writer = BufferedWriter(OutputStreamWriter(client.getOutputStream(), "UTF-8"))

    # Token handshake: first line must be {"token": "..."} when token is set.
    first = reader.readLine()
    if first is None:
        return
    try:
        hs = json.loads(first)
    except ValueError:
        hs = {}
    if token and hs.get("token") != token:
        writer.write(json.dumps({"ok": False, "error": "bad token"}) + "\n")
        writer.flush()
        return
    writer.write(json.dumps({"ok": True, "protocol": PROTOCOL_VERSION}) + "\n")
    writer.flush()

    while True:
        line = reader.readLine()
        if line is None:
            return
        response = _process_line(line + "\n")
        if response is None:
            continue
        writer.write(json.dumps(response) + "\n")
        writer.flush()


# ---- entry -------------------------------------------------------------

def parse_args(argv):
    args = {"transport": "stdio", "host": "127.0.0.1", "port": 0, "token": ""}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--transport" and i + 1 < len(argv):
            args["transport"] = argv[i + 1]
            i += 2
        elif a in ("--bind", "--host") and i + 1 < len(argv):
            args["host"] = argv[i + 1]
            i += 2
        elif a == "--port" and i + 1 < len(argv):
            args["port"] = int(argv[i + 1])
            i += 2
        elif a == "--token" and i + 1 < len(argv):
            args["token"] = argv[i + 1]
            i += 2
        else:
            i += 1
    return args


def _script_args(argv):
    # SikuliX puts script args after a literal `--`. If the separator
    # isn't present, assume argv is already trimmed.
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return list(argv)


def main(argv):
    args = parse_args(_script_args(argv))
    if args["transport"] == "stdio":
        serve_stdio()
    elif args["transport"] == "tcp":
        if args["port"] == 0:
            sys.stderr.write("[bridge] tcp transport requires --port\n")
            sys.exit(2)
        serve_tcp(args["host"], args["port"], args["token"])
    else:
        sys.stderr.write("[bridge] unknown transport: " + str(args["transport"]) + "\n")
        sys.exit(2)


# SikuliX's `-r` runs the script with __name__ == "__main__", same as
# CPython, so the standard guard works. Kept explicit for clarity.
if __name__ == "__main__":
    main(sys.argv)
