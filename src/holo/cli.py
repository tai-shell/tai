"""CLI surface for holo.

Subcommands:

    holo --version         print version
    holo windows           print visible windows (smoke for windows reader)
    holo doctor            check macOS permissions / runtime environment
    holo demo              end-to-end smoke test against the in-page agent
    holo mcp               run the MCP server over stdio
    holo mcp --listen PORT run the MCP server over TCP (single connection)
    holo connect HOST:PORT stdio↔TCP bridge to a listening `holo mcp`
    holo screen <verb>     smoke-test the SikuliX-backed screen tools directly
    holo install-screen    pre-download the SikuliX jar into the user cache
    holo install-bookmarklet  download the bookmarklet page and open it
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

from holo import __version__


def _cmd_windows() -> int:
    from holo.windows import list_windows

    try:
        windows = list_windows()
    except NotImplementedError as e:
        print(f"holo windows: {e}", file=sys.stderr)
        return 2
    if not windows:
        print("(no visible windows reported)")
        return 0
    for w in windows:
        title = w.title if w.title else "<unreadable>"
        print(f"{w.id:>8}  L{w.layer}  {w.owner!r:>24}  {title}")
    return 0


def _cmd_doctor() -> int:
    """Check the daemon's environment: platform, permissions, deps."""
    print(f"Python:    {sys.executable}")
    print(f"Platform:  {sys.platform}")
    print(f"Version:   holo {__version__}")
    print()

    if sys.platform != "darwin":
        print(f"⚠ holo currently supports macOS only; running on {sys.platform}.")
        return 1

    try:
        from holo.windows import list_windows
    except Exception as e:  # noqa: BLE001 — surface anything that breaks the import
        print(f"❌ holo.windows failed to import: {e}")
        return 1

    try:
        windows = list_windows()
    except Exception as e:  # noqa: BLE001
        print(f"❌ list_windows() raised: {e}")
        return 1

    from holo.channel import DEFAULT_BROWSERS

    total = len(windows)
    browser_wins = [w for w in windows if w.owner in DEFAULT_BROWSERS]
    browser_titled = sum(1 for w in browser_wins if w.title)
    print(f"Windows:   {total} visible total, {len(browser_wins)} from a browser")

    if total == 0:
        print()
        print("⚠ No visible windows reported. Is anything open?")
        return 1

    if not browser_wins:
        print()
        print("⚠ No browser windows visible. Open Chrome/Firefox/Safari/etc.")
        print("  before running `holo demo`.")
        return 1

    if browser_titled == 0:
        # System windows (e.g. WindowServer/StatusIndicator) have readable
        # titles even without Screen Recording permission, so we check
        # specifically that *browser* windows are readable.
        print()
        print("❌ Screen Recording permission appears to be missing.")
        print(f"   Grant access for: {sys.executable}")
        print("   System Settings → Privacy & Security → Screen Recording")
        print("   You may need to restart the daemon after granting.")
        return 1

    print(f"✓ Screen Recording permission granted ({browser_titled} browser titles readable).")
    print()

    try:
        import pyautogui  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"❌ pyautogui import failed: {e}")
        return 1
    try:
        import pyperclip  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"❌ pyperclip import failed: {e}")
        return 1
    print("✓ pyautogui and pyperclip importable.")
    print()
    print("Accessibility permission (for keyboard simulation) cannot be")
    print("detected without firing a keystroke. If `holo demo` fails to")
    print("get a reply, grant Accessibility for the same Python binary at:")
    print("  System Settings → Privacy & Security → Accessibility")
    return 0


_MANUAL_COUNTDOWN_S: int = 5


def _cmd_demo(*, manual: bool = False, hide_qr: bool = False, enable_screen: bool = False) -> int:
    """End-to-end smoke test: read R2D2_VERSION through the channel.

    Pass `manual=True` (or run `holo demo --manual`) to skip the
    automatic activate-and-click step and instead use a fixed
    countdown: you click into the popup body and don't touch the
    keyboard until the paste fires. Useful when cross-app activation
    is being denied by the OS.

    Pass `hide_qr=True` (or `holo demo --hide-qr`) to render the QR
    reply channel in two near-identical greens that humans / external
    cameras can't decode; the daemon amplifies the subtle red-channel
    delta in software just before running Vision QR detection.
    """
    import time

    from holo.channel import CalibrationError, CommandError
    from holo.daemon import Daemon

    print("holo demo — Phase 0 walking-skeleton" + (" (manual)" if manual else ""))
    print()
    print("Setup:")
    print("  1. (one-time) Build & install the bookmarklet:")
    print("       cd bookmarklet && npm install && npm run build")
    print("       open bookmarklet/dist/install.html")
    print("       drag the 🔧 holo link to your bookmarks bar")
    print("  2. (one-time) Allow popups for the host page in your browser.")
    print("  3. Open https://tai.sh (or any page exposing R2D2_VERSION).")
    print("  4. After this command starts polling, click the 🔧 holo bookmark.")
    print("     A small dark green 'holo console' popup will open — leave it.")
    if manual:
        print(
            f"     In manual mode, you'll have {_MANUAL_COUNTDOWN_S} s after"
            " calibration to click"
        )
        print(
            "     into the popup body. The paste fires automatically — don't"
        )
        print("     touch the keyboard once you've clicked.")
    else:
        print("     The daemon will raise it before each command, so you don't")
        print("     have to babysit focus.")
    print()
    print("Run `holo doctor` first if you suspect a permissions issue.")
    print()

    daemon = Daemon(hide_qr=hide_qr, enable_screen=enable_screen)
    if hide_qr:
        print("QR reply channel: stealth (camera-resistant)")
    if enable_screen:
        print("Screen tools: SikuliX bridge enabled")
    print(f"WS listener: {daemon.ws_server.url}")
    print("Polling for calibration beacon (60s timeout)…")
    try:
        ch = daemon.calibrate(timeout=60.0)
    except CalibrationError as e:
        print(f"❌ {e}", file=sys.stderr)
        print(
            "   Is the bookmarklet installed and clicked on a normal http(s) page?",
            file=sys.stderr,
        )
        print("   Run `holo doctor` to check Screen Recording permission.", file=sys.stderr)
        return 1

    print(f"✓ calibrated · session={ch.session} window={ch._window_id}")

    if manual:
        # Disable the auto activate+click so the user can drive focus
        # by hand. We countdown without reading stdin — pressing Enter
        # would steal focus back from the popup.
        ch._window_pid = 0
        print()
        print("Manual mode: click anywhere in the GREEN popup body NOW.")
        print("Don't touch the keyboard or any other window.")
        print("The paste will fire automatically after the countdown.")
        for i in range(_MANUAL_COUNTDOWN_S, 0, -1):
            print(f"  {i}…", flush=True)
            time.sleep(1.0)

    print()
    print("Sending read_global(document.title)…")
    try:
        title_result = ch.send_command(
            {"op": "read_global", "path": "document.title"}, timeout=10.0
        )
    except CommandError as e:
        print(f"❌ {e}", file=sys.stderr)
        print("   Possible causes:", file=sys.stderr)
        print("   - The popup didn't have OS keyboard focus when Cmd+V fired", file=sys.stderr)
        print("     (try `holo demo --manual` and click the popup body yourself)", file=sys.stderr)
        print(
            "   - Accessibility permission missing (System Settings → Privacy & Security)",
            file=sys.stderr,
        )
        print("   - osascript Automation prompt was denied — re-allow in", file=sys.stderr)
        print("     System Settings → Privacy & Security → Automation", file=sys.stderr)
        return 1

    transport = "ws" if ch._ws_ready else "qr"
    print(f"✓ title: {title_result}  (transport: {transport})")

    print("Sending read_global(R2D2_VERSION)…")
    try:
        result = ch.send_command(
            {"op": "read_global", "path": "R2D2_VERSION"}, timeout=10.0
        )
    except CommandError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    print(f"✓ result: {result}  (transport: {transport})")

    # If WS came up, send a second command — should be near-instant and
    # not steal focus. Confirms the post-handshake hot path works.
    if ch._ws_ready:
        print("Re-sending over WS to confirm hot path…")
        try:
            result2 = ch.send_command(
                {"op": "read_global", "path": "R2D2_VERSION"}, timeout=5.0
            )
            print(f"✓ result: {result2}  (transport: ws)")
        except CommandError as e:
            print(f"❌ second send failed: {e}", file=sys.stderr)
            return 1
    return 0


def _cmd_focus() -> int:
    """Diagnostic: activate + click into the locked window's body, no paste.

    Calibrates against the live bookmarklet, then runs the same
    activate-and-click sequence the demo uses before pasting — without
    sending any keystroke. Lets you see *visually* whether the popup
    comes to the foreground and whether the click hits the body.
    """
    import time

    from holo.channel import CalibrationError, Channel

    print("holo focus — diagnostic activate-and-click (no paste)")
    print()
    print("Open the holo console popup first (run holo demo, click the bookmark)")
    print("and leave it visible. This command will calibrate against it, then")
    print("activate + click. Watch whether the popup comes to the front and")
    print("whether the click lands inside the green body.")
    print()

    ch = Channel(default_timeout=15.0)
    print("Polling for calibration beacon (15s timeout)…")
    try:
        sid = ch.wait_for_calibration()
    except CalibrationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    print(f"✓ calibrated · session={sid} window={ch._window_id} pid={ch._window_pid}")
    print()
    print("Activating + clicking in 3s — watch the popup…")
    time.sleep(3.0)
    ch._activate_target()
    print("✓ done. Did the popup come to the front and receive the click?")
    return 0


def _cmd_mcp(
    *,
    hide_qr: bool = False,
    enable_screen: bool = False,
    no_bookmarklet: bool = False,
    no_browser: bool = False,
    input_proxy: tuple[str, int] | None = None,
    remote_screen: tuple[str, int] | None = None,
    listen_port: int | None = None,
    announce: bool = False,
    announce_session: str | None = None,
    announce_user: str | None = None,
    announce_ssh_user: str | None = None,
    announce_ips: list[str] | None = None,
    announce_capabilities: bool = False,
    announce_command: str | None = None,
    auto_tunnel: bool = False,
    auto_tunnel_backend: str | None = None,
) -> int:
    """Run the MCP server.

    Default mode is stdio — intended to be launched by an MCP client
    (Claude Code, Codex, Cursor) rather than from a terminal.

    With `--listen PORT`, instead binds 127.0.0.1:PORT and accepts a
    single concurrent TCP client. Each new connection must send the
    magic handshake prefix before any MCP traffic, so a drive-by
    browser can't reach the server (browsers can't control the
    first bytes of a TCP connection — fetch always sends an HTTP
    request line first). Daemon state persists across reconnects.

    With `--no-bookmarklet`, the channel-dependent tools are
    omitted from the surface and the WS server isn't started. Use
    this for agents that only drive screen / template / AppleScript
    tools — e.g. a Slack-only orchestrator.

    With `--announce`, broadcasts an mDNS service record so a
    companion desktop app on the same LAN can discover this session.
    Optional metadata: `--announce-session NAME` (logical session
    id), `--announce-user NAME` (display label, defaults to $USER),
    `--announce-ssh-user NAME` (SSH login user, omitted if not set),
    `--announce-ip A,B,C` (comma-separated IPv4 list; each entry is
    either a literal IP that's advertised verbatim or a trailing-dot
    prefix like `192.168.1.` that filters the enumerated interfaces.
    Default is to enumerate every non-loopback interface).

    Either way we print only to stderr — stdout carries protocol.
    """
    from holo import mcp_server

    def _banner_lines() -> list[str]:
        lines: list[str] = []
        if hide_qr:
            lines.append("QR reply channel: stealth (camera-resistant)")
        if enable_screen:
            lines.append("Screen tools: SikuliX bridge enabled")
        if no_bookmarklet:
            lines.append("Bookmarklet channel: disabled (--no-bookmarklet)")
        if no_browser:
            lines.append("Browser AppleScript tools: disabled (--no-browser)")
        if input_proxy is not None:
            lines.append(
                f"Input proxy: events route to remote holo at "
                f"{input_proxy[0]}:{input_proxy[1]} (--input-proxy / --remote-input)"
            )
        if remote_screen is not None:
            same = input_proxy == remote_screen
            tag = " (shared connection)" if same else ""
            lines.append(
                f"Screen proxy: captures route to remote holo at "
                f"{remote_screen[0]}:{remote_screen[1]} (--remote-screen){tag}"
            )
        if announce:
            label_bits = []
            if announce_session:
                label_bits.append(f"session={announce_session}")
            if announce_user:
                label_bits.append(f"user={announce_user}")
            if announce_ips:
                label_bits.append(f"ips={','.join(announce_ips)}")
            label = " ".join(label_bits) if label_bits else "(defaults)"
            lines.append(f"mDNS announce: enabled — {label}")
        if announce_capabilities:
            lines.append(
                "Capabilities endpoint: enabled "
                "(applications + packages auto-discovered per platform)"
            )
        if auto_tunnel:
            backend_label = (
                auto_tunnel_backend
                or "$HOLO_BACKEND or default"
            )
            lines.append(
                f"Auto-tunnel: enabled — opens reverse SSH forwards into "
                f"every discovered CloudCity. Cert backend: {backend_label}"
            )
        return lines

    announce_kwargs: dict[str, Any] = {
        "announce": announce,
        "announce_session": announce_session,
        "announce_user": announce_user,
        "announce_ssh_user": announce_ssh_user,
        "announce_ips": announce_ips,
        "announce_capabilities": announce_capabilities,
        "announce_command": announce_command,
        "auto_tunnel": auto_tunnel,
        "auto_tunnel_backend": auto_tunnel_backend,
    }

    if listen_port is not None:
        print(
            f"holo mcp — listening on 127.0.0.1:{listen_port} "
            "(magic prefix required)",
            file=sys.stderr,
        )
        for line in _banner_lines():
            print(line, file=sys.stderr)
        mcp_server.run_tcp(
            listen_port,
            hide_qr=hide_qr,
            enable_screen=enable_screen,
            no_bookmarklet=no_bookmarklet,
            no_browser=no_browser,
            input_proxy=input_proxy,
            remote_screen=remote_screen,
            **announce_kwargs,
        )
        return 0

    print("holo mcp — starting MCP server over stdio", file=sys.stderr)
    for line in _banner_lines():
        print(line, file=sys.stderr)
    mcp_server.run(
        hide_qr=hide_qr,
        enable_screen=enable_screen,
        no_bookmarklet=no_bookmarklet,
        no_browser=no_browser,
        input_proxy=input_proxy,
        remote_screen=remote_screen,
        **announce_kwargs,
    )
    return 0


def _cmd_connect(rest: list[str]) -> int:
    """Bridge process stdio to a listening `holo mcp --listen PORT`.

    Used as the remote side of an SSH-tunnelled MCP setup:

        holo mcp-remote -- ssh user@host /usr/local/bin/holo connect localhost:7777

    The magic handshake prefix is sent automatically — users never
    type it.
    """
    from holo import mcp_connect

    if len(rest) != 1 or rest[0] in {"-h", "--help"}:
        sys.stderr.write(
            "usage: holo connect HOST:PORT\n"
            "example: holo connect localhost:7777\n"
        )
        return 2
    return mcp_connect.run(rest[0])


def _cmd_mcp_remote(rest: list[str]) -> int:
    """Bridge a local MCP client's stdio to a remote `holo mcp`.

    Spawns the user-supplied command (everything after `--`), strips
    stdout banner content until a JSON envelope arrives, then becomes a
    transparent stdio proxy. See `holo.mcp_remote` for the proxy
    semantics. Typical usage:

        holo mcp-remote -- ssh -A hostA holo mcp
        holo mcp-remote -- kubectl exec -i pod-x -- holo mcp
    """
    from holo import mcp_remote

    if "--" not in rest:
        sys.stderr.write(
            "usage: holo mcp-remote [--startup-timeout SECS] -- <command>\n"
            "example: holo mcp-remote -- ssh -A hostA holo mcp\n"
        )
        return 2
    sep = rest.index("--")
    flags = rest[:sep]
    child_argv = rest[sep + 1:]

    timeout = mcp_remote.DEFAULT_STARTUP_TIMEOUT_S
    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag == "--startup-timeout" and i + 1 < len(flags):
            try:
                timeout = float(flags[i + 1])
            except ValueError:
                sys.stderr.write(
                    f"holo mcp-remote: invalid --startup-timeout: {flags[i + 1]!r}\n"
                )
                return 2
            i += 2
        else:
            sys.stderr.write(f"holo mcp-remote: unknown flag: {flag!r}\n")
            return 2

    return mcp_remote.run(child_argv, startup_timeout_s=timeout)


_SCREEN_USAGE = (
    "usage: holo screen <verb> [args]\n"
    "  ping                          start the JVM bridge and ping it\n"
    "  activate <app>                bring an app to the foreground\n"
    "  click <x> <y>                 click at screen coordinates\n"
    "  key <combo>                   send a key combo (e.g. 'cmd+v', 'enter')\n"
    "  type <text>                   type a literal string"
)


def _cmd_screen(rest: list[str]) -> int:
    """Drive the SikuliX-backed screen tools directly — no channel.

    This is for sanity-checking the JVM + jar setup end-to-end. Each
    invocation spawns a fresh JVM, runs one verb, exits. Slow for
    everyday use; that's fine, this is a smoke tool.
    """
    from holo.bridge import BridgeClient, BridgeError, BridgeMissingError

    if not rest:
        print(_SCREEN_USAGE, file=sys.stderr)
        return 2
    verb = rest[0]
    args = rest[1:]

    client = BridgeClient()
    try:
        client.start()
    except BridgeMissingError as e:
        print(f"holo screen: {e}", file=sys.stderr)
        print(
            "Hint: install OpenJDK 11+ and drop sikulixapi.jar in vendor/ "
            "or set HOLO_SIKULI_JAR.",
            file=sys.stderr,
        )
        return 1
    except (BridgeError, OSError) as e:
        print(f"holo screen: failed to start JVM: {e}", file=sys.stderr)
        return 1

    try:
        if verb == "ping":
            result = client.ping()
        elif verb == "activate" and len(args) == 1:
            result = client.activate(args[0])
        elif verb == "click" and len(args) == 2:
            result = client.click(int(args[0]), int(args[1]))
        elif verb == "key" and len(args) == 1:
            result = client.key(args[0])
        elif verb == "type" and len(args) >= 1:
            result = client.type_text(" ".join(args))
        else:
            print(_SCREEN_USAGE, file=sys.stderr)
            return 2
    except BridgeError as e:
        print(f"holo screen: error {e.code}: {e.message}", file=sys.stderr)
        if e.trace:
            print(e.trace, file=sys.stderr)
        return 1
    finally:
        client.stop()

    print(result)
    return 0


def _cmd_install_screen() -> int:
    """Pre-download the SikuliX jar into the user cache.

    Useful in air-gapped / metered-bandwidth environments where you want
    to stage the jar up front rather than letting the daemon fetch it on
    first run. Idempotent: if the cached file is already valid, exits
    immediately.
    """
    from holo.bridge import (
        SIKULI_JAR_BYTES,
        SIKULI_JAR_NAME,
        SIKULI_JAR_URL,
        BridgeMissingError,
        ensure_jar,
    )

    print(f"holo install-screen — fetching {SIKULI_JAR_NAME}")
    print(f"  source: {SIKULI_JAR_URL}")

    last_pct = {"value": -1}

    def on_progress(read: int, total: int) -> None:
        if not total:
            return
        pct = int(100 * read / total)
        if pct != last_pct["value"]:
            last_pct["value"] = pct
            sys.stdout.write(
                f"\r  progress: {pct:3d}%  ({read / 1_048_576:.1f} / "
                f"{total / 1_048_576:.1f} MiB)"
            )
            sys.stdout.flush()

    try:
        path = ensure_jar(on_progress=on_progress)
    except BridgeMissingError as e:
        print()
        print(f"holo install-screen: {e}", file=sys.stderr)
        return 1
    finally:
        if last_pct["value"] >= 0:
            print()
    print(f"✓ cached at {path}")
    print(f"  size:   {path.stat().st_size} bytes (pinned: {SIKULI_JAR_BYTES})")
    return 0


def _cmd_ide() -> int:
    """Launch the SikuliX IDE (`java -jar sikulixide-*.jar`).

    Downloads the jar on first use (same path as `holo install-screen`)
    so this is one self-contained command. Spawns the JVM as a child
    subprocess of holo (NOT exec'd in-place): macOS TCC attributes
    Accessibility / Screen-Recording / Input-Monitoring permission
    checks to the responsible parent process, so the Accessibility
    grant the user has already given to the `holo` binary covers the
    SikuliX IDE too. `os.execvp` would replace holo's identity with
    `java` and silently lose all permissions — the IDE window would
    still appear but mouse / keyboard simulation would not work.
    """
    import shutil

    from holo.bridge import (
        SIKULI_JAR_NAME,
        SIKULI_JAR_URL,
        BridgeMissingError,
        ensure_jar,
    )

    java = shutil.which("java")
    if java is None:
        print(
            "holo ide: `java` not on PATH. Install OpenJDK 11+ "
            "(e.g. `brew install openjdk@21`) and re-run.",
            file=sys.stderr,
        )
        return 1

    last_pct = {"value": -1}

    def on_progress(read: int, total: int) -> None:
        if not total:
            return
        pct = int(100 * read / total)
        if pct != last_pct["value"]:
            last_pct["value"] = pct
            sys.stdout.write(
                f"\rholo ide — fetching {SIKULI_JAR_NAME}: {pct:3d}%  "
                f"({read / 1_048_576:.1f} / {total / 1_048_576:.1f} MiB)"
            )
            sys.stdout.flush()

    try:
        jar_path = ensure_jar(on_progress=on_progress)
    except BridgeMissingError as e:
        if last_pct["value"] >= 0:
            print()
        print(f"holo ide: {e}\n  source: {SIKULI_JAR_URL}", file=sys.stderr)
        return 1
    if last_pct["value"] >= 0:
        print()  # finish the progress line

    argv = [java, "-jar", str(jar_path)]
    return subprocess.run(argv).returncode


def _cmd_install_bookmarklet(rest: list[str]) -> int:
    """Download `holo-bookmarklet.html` from the matching release and
    open it in the default browser."""
    from holo import install_bookmarklet

    url: str | None = None
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag == "--url" and i + 1 < len(rest):
            url = rest[i + 1]
            i += 2
        else:
            sys.stderr.write(
                f"holo install-bookmarklet: unknown flag: {flag!r}\n"
                "usage: holo install-bookmarklet [--url URL]\n"
            )
            return 2
    return install_bookmarklet.run(url=url)


_DISCOVER_USAGE = (
    "usage: holo discover [--json | --tail | --serve PORT] [options]\n"
    "  --json                  one-shot snapshot, JSON array, exit\n"
    "  --tail                  long-running JSONL event stream\n"
    "  --serve PORT            HTTP+WebSocket server (default 7082)\n"
    "  --wait SECS             --json browse window (default 3.0)\n"
    "  --stale-after SECS      drop sessions older than this (default 150)\n"
    "  --rebrowse-interval S   --serve: rebuild the zeroconf browser every\n"
    "                          S seconds (default 300, 0 disables). Store\n"
    "                          is preserved across the swap so /sessions\n"
    "                          stays continuous; defends against silent\n"
    "                          browse stalls.\n"
    "  --cors-origin O         comma-separated CORS allow-list "
    "(default: http://localhost:8888,https://app-dev.tai.sh)"
)


def _cmd_discover(rest: list[str]) -> int:
    """Browse the LAN for `_holo-session._tcp.local.` broadcasts.

    Reference consumer of `docs/companion-spec.md`. See `--help` for
    the three output modes (`--json`, `--tail`, `--serve PORT`).
    """
    from holo import discover

    json_mode = "--json" in rest
    tail_mode = "--tail" in rest
    serve_port_raw = _value_flag(rest, "--serve")

    selected = sum([json_mode, tail_mode, serve_port_raw is not None])
    if selected == 0:
        sys.stderr.write(
            "holo discover: pick exactly one mode (--json | --tail | --serve PORT)\n"
            f"{_DISCOVER_USAGE}\n"
        )
        return 2
    if selected > 1:
        sys.stderr.write(
            "holo discover: --json / --tail / --serve are mutually exclusive\n"
        )
        return 2

    if serve_port_raw is _MISSING_ARG:
        sys.stderr.write("holo discover: --serve requires a port number\n")
        return 2

    wait_raw = _value_flag(rest, "--wait")
    if wait_raw is _MISSING_ARG:
        sys.stderr.write("holo discover: --wait requires a value\n")
        return 2
    wait_s = discover.DEFAULT_JSON_WAIT_S
    if isinstance(wait_raw, str):
        try:
            wait_s = float(wait_raw)
        except ValueError:
            sys.stderr.write(
                f"holo discover: invalid --wait value {wait_raw!r}\n"
            )
            return 2

    stale_raw = _value_flag(rest, "--stale-after")
    if stale_raw is _MISSING_ARG:
        sys.stderr.write("holo discover: --stale-after requires a value\n")
        return 2
    stale_after_s = discover.DEFAULT_STALE_AFTER_S
    if isinstance(stale_raw, str):
        try:
            stale_after_s = float(stale_raw)
        except ValueError:
            sys.stderr.write(
                f"holo discover: invalid --stale-after value {stale_raw!r}\n"
            )
            return 2

    cors_raw = _value_flag(rest, "--cors-origin")
    if cors_raw is _MISSING_ARG:
        sys.stderr.write("holo discover: --cors-origin requires a value\n")
        return 2
    cors_origins: list[str] | None = None
    if isinstance(cors_raw, str):
        cors_origins = [
            o.strip() for o in cors_raw.split(",") if o.strip()
        ]
        if not cors_origins:
            sys.stderr.write(
                "holo discover: --cors-origin requires at least one origin\n"
            )
            return 2

    rebrowse_raw = _value_flag(rest, "--rebrowse-interval")
    if rebrowse_raw is _MISSING_ARG:
        sys.stderr.write(
            "holo discover: --rebrowse-interval requires a value\n"
        )
        return 2
    rebrowse_interval_s = discover.DEFAULT_REBROWSE_INTERVAL_S
    if isinstance(rebrowse_raw, str):
        try:
            rebrowse_interval_s = float(rebrowse_raw)
        except ValueError:
            sys.stderr.write(
                f"holo discover: invalid --rebrowse-interval value "
                f"{rebrowse_raw!r}\n"
            )
            return 2
        if rebrowse_interval_s < 0:
            sys.stderr.write(
                "holo discover: --rebrowse-interval must be >= 0 "
                "(0 disables)\n"
            )
            return 2

    if json_mode:
        return discover.run_oneshot(wait_s=wait_s)
    if tail_mode:
        return discover.run_tail(stale_after_s=stale_after_s)
    # --serve
    assert isinstance(serve_port_raw, str)  # _value_flag already returned a str
    try:
        port = int(serve_port_raw)
    except ValueError:
        sys.stderr.write(
            f"holo discover: invalid --serve port {serve_port_raw!r}\n"
        )
        return 2
    if not (0 < port < 65536):
        sys.stderr.write(f"holo discover: --serve port {port} out of range\n")
        return 2
    return discover.run_serve(
        port=port,
        cors_origins=cors_origins,
        stale_after_s=stale_after_s,
        rebrowse_interval_s=rebrowse_interval_s,
    )


_CLOUDCITY_USAGE = """\
holo cloudcity announce
        [--port PORT] [--ips A,B,...] [--backend URL]
        [--ca-fps FP[,FP...]] [--instance NAME]

  Broadcasts a `_cloudcity._tcp.local.` mDNS service so holo daemons
  on the LAN can discover this CloudCity host (i.e. the c2w-net
  Docker container's exposed sshd) and set up reverse SSH tunnels
  into it. Foreground process; Ctrl-C unregisters cleanly.

  --port PORT         c2w-net's exposed sshd port (default: 2222).
  --ips A,B,...       IPv4 override; each entry is a literal IP or a
                      trailing-dot prefix (e.g. `192.168.1.`) used to
                      filter enumerated interfaces. Default: auto.
  --backend URL       Where holo daemons can fetch certs that c2w-net
                      will accept. Default: omit. Use
                      `http://localhost:8081` for `make backend-local`.
  --ca-fps FP[,FP...] Comma-separated SHA256 fingerprints of CA
                      pubkeys c2w-net trusts. Default: auto-fetch
                      from --backend's /v1/ssh/ca endpoint when
                      --backend is set.
  --instance NAME     Override the auto-generated mDNS instance label
                      (default: cloudcity-<hostname>-<random6>).

holo cloudcity discover [--json | --tail] [--wait SECS] [--stale-after SECS]

  Subscribes to `_cloudcity._tcp.local.` and emits records.

  --json              one-shot: browse for SECS, print JSON array, exit
  --tail              long-running JSONL event stream (add/update/remove)
  --wait SECS         --json buffer time (default: 3.0)
  --stale-after SECS  drop entries last_seen older than this (default: 150)

  HTTP/WS surface lives inside `holo discover --serve PORT` —
  this same daemon also exposes `GET /cloudcities`.

  Spec:
    https://github.com/bradclarkalexander/desktop/blob/develop/docs/holo-cloudcity-tunnel-spec.md
"""


def _cmd_cloudcity(rest: list[str]) -> int:
    if not rest:
        sys.stderr.write(
            "holo cloudcity: missing subcommand\n" + _CLOUDCITY_USAGE
        )
        return 2
    sub = rest[0]
    sub_rest = rest[1:]
    if sub in {"-h", "--help", "help"}:
        print(_CLOUDCITY_USAGE)
        return 0
    if sub == "announce":
        return _cmd_cloudcity_announce(sub_rest)
    if sub == "discover":
        return _cmd_cloudcity_discover(sub_rest)
    sys.stderr.write(
        f"holo cloudcity: unknown subcommand {sub!r}\n" + _CLOUDCITY_USAGE
    )
    return 2


def _cmd_cloudcity_discover(rest: list[str]) -> int:
    """Run `holo cloudcity discover` in --json or --tail mode."""
    from holo import cloudcity_discover

    json_mode = "--json" in rest
    tail_mode = "--tail" in rest

    selected = sum([json_mode, tail_mode])
    if selected == 0:
        sys.stderr.write(
            "holo cloudcity discover: pick exactly one mode (--json | --tail)\n"
        )
        return 2
    if selected > 1:
        sys.stderr.write(
            "holo cloudcity discover: --json / --tail are mutually exclusive\n"
        )
        return 2

    wait_raw = _value_flag(rest, "--wait")
    if wait_raw is _MISSING_ARG:
        sys.stderr.write("holo cloudcity discover: --wait requires a value\n")
        return 2
    wait_s = cloudcity_discover.DEFAULT_JSON_WAIT_S
    if isinstance(wait_raw, str):
        try:
            wait_s = float(wait_raw)
        except ValueError:
            sys.stderr.write(
                f"holo cloudcity discover: invalid --wait value {wait_raw!r}\n"
            )
            return 2

    stale_raw = _value_flag(rest, "--stale-after")
    if stale_raw is _MISSING_ARG:
        sys.stderr.write(
            "holo cloudcity discover: --stale-after requires a value\n"
        )
        return 2
    stale_after_s = cloudcity_discover.DEFAULT_STALE_AFTER_S
    if isinstance(stale_raw, str):
        try:
            stale_after_s = float(stale_raw)
        except ValueError:
            sys.stderr.write(
                f"holo cloudcity discover: invalid --stale-after value "
                f"{stale_raw!r}\n"
            )
            return 2

    if json_mode:
        return cloudcity_discover.run_oneshot(wait_s=wait_s)
    return cloudcity_discover.run_tail(stale_after_s=stale_after_s)


def _cmd_cloudcity_announce(rest: list[str]) -> int:
    """Run `holo cloudcity announce` until interrupted."""
    from holo import cloudcity_announce

    port_raw = _value_flag(rest, "--port")
    ips_raw = _value_flag(rest, "--ips")
    backend_raw = _value_flag(rest, "--backend")
    ca_fps_raw = _value_flag(rest, "--ca-fps")
    instance_raw = _value_flag(rest, "--instance")

    if port_raw is _MISSING_ARG:
        sys.stderr.write("holo cloudcity announce: --port requires a value\n")
        return 2
    if ips_raw is _MISSING_ARG:
        sys.stderr.write("holo cloudcity announce: --ips requires a value\n")
        return 2
    if backend_raw is _MISSING_ARG:
        sys.stderr.write("holo cloudcity announce: --backend requires a value\n")
        return 2
    if ca_fps_raw is _MISSING_ARG:
        sys.stderr.write("holo cloudcity announce: --ca-fps requires a value\n")
        return 2
    if instance_raw is _MISSING_ARG:
        sys.stderr.write("holo cloudcity announce: --instance requires a value\n")
        return 2

    port = cloudcity_announce.DEFAULT_PORT
    if isinstance(port_raw, str):
        try:
            port = int(port_raw)
        except ValueError:
            sys.stderr.write(
                f"holo cloudcity announce: invalid --port {port_raw!r}\n"
            )
            return 2
        if not (0 < port < 65536):
            sys.stderr.write(
                f"holo cloudcity announce: --port {port} out of range\n"
            )
            return 2

    ips: list[str] | None = None
    if isinstance(ips_raw, str):
        ips = [s.strip() for s in ips_raw.split(",") if s.strip()]
        if not ips:
            sys.stderr.write(
                "holo cloudcity announce: --ips requires at least one entry\n"
            )
            return 2

    backend: str | None = None
    if isinstance(backend_raw, str):
        backend = backend_raw

    ca_fps: list[str] | None = None
    if isinstance(ca_fps_raw, str):
        ca_fps = [s.strip() for s in ca_fps_raw.split(",") if s.strip()]
        if not ca_fps:
            sys.stderr.write(
                "holo cloudcity announce: --ca-fps requires at least one entry\n"
            )
            return 2

    instance: str | None = None
    if isinstance(instance_raw, str):
        instance = instance_raw

    announcer = cloudcity_announce.CloudCityAnnouncer(
        port=port,
        ips=ips,
        backend=backend,
        ca_fps=ca_fps,
        instance=instance,
    )

    try:
        announcer.start()
    except Exception as e:  # noqa: BLE001 - surface any zeroconf failure to user
        sys.stderr.write(
            f"holo cloudcity announce: failed to start: {e}\n"
        )
        return 1

    info = announcer._service_info
    label = info.name.removesuffix("." + cloudcity_announce.SERVICE_TYPE) if info else "?"
    print(f"CloudCity announce: {label} on port {port}")
    if backend:
        print(f"  backend: {backend}")
    advertised_ips = announcer._collect_ips()
    if advertised_ips:
        print(f"  ips: {','.join(advertised_ips)}")
    print("  (Ctrl-C to stop)")

    # Wait for SIGINT/SIGTERM. signal.pause() is POSIX-only — holo is
    # macOS-first so this is fine, and Linux is OK too. Windows isn't
    # supported for this subcommand.
    import signal

    def _on_term(_signum: int, _frame: object) -> None:
        # SIGTERM should exit cleanly. KeyboardInterrupt covers SIGINT.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        announcer.stop()
    return 0


_CERT_USAGE = """\
holo cert show    [--key-path PATH]
holo cert get     [--backend URL] [--key-path PATH]
holo cert refresh [--backend URL] [--key-path PATH]

  Manage this holo daemon's outbound-SSH cert. Used by `holo tunnel up`
  (Phase 4) to authenticate to a CloudCity host's sshd as `lando`,
  which c2w-net's TrustedUserCAKeys accepts based on a CA the backend
  controls — no per-c2w-net pubkey enrollment required.

  show        Print on-disk state (key fingerprint, cert validity,
              CA pubkey, last refresh time). No network access.
  get         Generate the keypair if missing, then fetch a cert from
              <backend>/v1/ssh/sign. Idempotent — re-uses an existing
              fresh cert.
  refresh     Same as `get` but always re-fetches, even if the current
              cert is still within its validity window.

  --backend URL    Backend cert-signing endpoint. Precedence:
                     1. --backend URL (this flag)
                     2. HOLO_BACKEND env var
                     3. https://api-dev.tai.sh (default)
                   For local-dev against `make backend-local`, pass
                   `--backend http://localhost:8081`.

  --key-path PATH  Override the daemon's keypair location. Default:
                   ~/.holo/host-key. Companion files live alongside:
                     <key>      private key
                     <key>.pub  public key
                     <key>-cert.pub   signed cert (presented by ssh)
                     <key>-cert.json  metadata sidecar (validity, etc.)

  In LOCAL_DEV_MODE the backend's /v1/ssh/sign endpoint accepts
  unauthenticated requests; cert principal is hardcoded to `lando`.
  Production (api-dev / api) auth is on the deferred list.

  Spec: §4.3 of
    https://github.com/bradclarkalexander/desktop/blob/develop/docs/holo-cloudcity-tunnel-spec.md
"""


def _cmd_cert(rest: list[str]) -> int:
    if not rest:
        sys.stderr.write("holo cert: missing subcommand\n" + _CERT_USAGE)
        return 2
    sub = rest[0]
    sub_rest = rest[1:]
    if sub in {"-h", "--help", "help"}:
        print(_CERT_USAGE)
        return 0
    if sub == "show":
        return _cmd_cert_show(sub_rest)
    if sub in {"get", "refresh"}:
        return _cmd_cert_fetch(sub_rest, force=(sub == "refresh"))
    sys.stderr.write(
        f"holo cert: unknown subcommand {sub!r}\n" + _CERT_USAGE
    )
    return 2


def _cert_key_path_from_flag(rest: list[str]) -> Any:
    """Parse --key-path; returns None on absence, _MISSING_ARG on empty,
    or a str on success."""
    return _value_flag(rest, "--key-path")


def _cmd_cert_show(rest: list[str]) -> int:
    """Read-only inspection of the on-disk cert state."""
    import json as _json
    from pathlib import Path

    from holo import cert as cert_mod

    key_path_raw = _cert_key_path_from_flag(rest)
    if key_path_raw is _MISSING_ARG:
        sys.stderr.write("holo cert show: --key-path requires a value\n")
        return 2
    key_path = (
        Path(key_path_raw).expanduser()
        if isinstance(key_path_raw, str)
        else cert_mod.DEFAULT_KEY_PATH
    )
    status = cert_mod.cert_status(key_path)
    print(_json.dumps(status, indent=2, default=str))
    return 0


def _cmd_cert_fetch(rest: list[str], *, force: bool) -> int:
    """Generate keypair (if missing) + fetch cert from backend."""
    import json as _json
    from pathlib import Path

    from holo import cert as cert_mod

    backend_raw = _value_flag(rest, "--backend")
    if backend_raw is _MISSING_ARG:
        sys.stderr.write("holo cert: --backend requires a value\n")
        return 2

    key_path_raw = _cert_key_path_from_flag(rest)
    if key_path_raw is _MISSING_ARG:
        sys.stderr.write("holo cert: --key-path requires a value\n")
        return 2
    key_path = (
        Path(key_path_raw).expanduser()
        if isinstance(key_path_raw, str)
        else cert_mod.DEFAULT_KEY_PATH
    )

    backend = backend_raw if isinstance(backend_raw, str) else None
    try:
        status = cert_mod.get_or_refresh(
            backend=backend, key_path=key_path, force=force
        )
    except cert_mod.CertFetchError as e:
        sys.stderr.write(f"holo cert: {e}\n")
        return 1
    except subprocess.CalledProcessError as e:
        # ssh-keygen failure
        stderr_excerpt = (e.stderr or b"").decode("utf-8", errors="replace")
        sys.stderr.write(
            f"holo cert: ssh-keygen failed (exit {e.returncode}): "
            f"{stderr_excerpt}\n"
        )
        return 1
    print(_json.dumps(status, indent=2, default=str))
    return 0


_TUNNEL_USAGE = """\
holo tunnel up <cloudcity-instance>
        [--backend URL] [--key-path PATH] [--principal NAME]
        [--discover-wait SECS]

  Establishes a reverse SSH tunnel from this holo daemon to the named
  CloudCity host. After the tunnel is up the daemon's local sshd
  (port 22) is reachable inside the CloudCity's c2w-net loopback at
  the announced `tunnel_port`. Foreground process; Ctrl-C tears down
  cleanly.

  <cloudcity-instance>  mDNS instance label (e.g. cloudcity-MacBook-3-abc)
                        OR hostname (matched against the `host` TXT
                        field). Use `holo cloudcity discover --json`
                        to list candidates.

  --backend URL         Cert-signing backend. Falls back to
                        HOLO_BACKEND env var, then https://api-dev.tai.sh.
                        For local-dev: http://localhost:8081.
  --key-path PATH       Override the daemon keypair path (default:
                        ~/.holo/host-key).
  --principal NAME      Override the cert principal (default: lando,
                        which c2w-net's TrustedUserCAKeys accepts).
                        Also overrideable via HOLO_TUNNEL_PRINCIPAL.
  --discover-wait SECS  How long to wait for the CloudCity record to
                        show up in mDNS before giving up (default: 1.5).

  Spec: §4.4 of
    https://github.com/bradclarkalexander/desktop/blob/develop/docs/holo-cloudcity-tunnel-spec.md
"""


def _cmd_tunnel(rest: list[str]) -> int:
    if not rest:
        sys.stderr.write("holo tunnel: missing subcommand\n" + _TUNNEL_USAGE)
        return 2
    sub = rest[0]
    sub_rest = rest[1:]
    if sub in {"-h", "--help", "help"}:
        print(_TUNNEL_USAGE)
        return 0
    if sub == "up":
        return _cmd_tunnel_up(sub_rest)
    sys.stderr.write(
        f"holo tunnel: unknown subcommand {sub!r}\n" + _TUNNEL_USAGE
    )
    return 2


def _cmd_tunnel_up(rest: list[str]) -> int:
    """Bring up a reverse tunnel to the named CloudCity. Foreground process."""
    import json as _json
    import signal
    from pathlib import Path

    from holo import cert as cert_mod
    from holo import tunnel as tunnel_mod

    # First positional, before any flags, is the CloudCity instance.
    positional: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--"):
            break
        positional.append(tok)
        i += 1
    if len(positional) != 1:
        sys.stderr.write(
            "holo tunnel up: expected exactly one positional argument "
            "(<cloudcity-instance>)\n"
        )
        return 2
    instance = positional[0]
    flag_rest = rest[i:]

    backend_raw = _value_flag(flag_rest, "--backend")
    if backend_raw is _MISSING_ARG:
        sys.stderr.write("holo tunnel up: --backend requires a value\n")
        return 2
    key_path_raw = _value_flag(flag_rest, "--key-path")
    if key_path_raw is _MISSING_ARG:
        sys.stderr.write("holo tunnel up: --key-path requires a value\n")
        return 2
    principal_raw = _value_flag(flag_rest, "--principal")
    if principal_raw is _MISSING_ARG:
        sys.stderr.write("holo tunnel up: --principal requires a value\n")
        return 2
    discover_wait_raw = _value_flag(flag_rest, "--discover-wait")
    if discover_wait_raw is _MISSING_ARG:
        sys.stderr.write(
            "holo tunnel up: --discover-wait requires a value\n"
        )
        return 2

    backend = backend_raw if isinstance(backend_raw, str) else None
    key_path = (
        Path(key_path_raw).expanduser()
        if isinstance(key_path_raw, str)
        else cert_mod.DEFAULT_KEY_PATH
    )
    principal = (
        principal_raw
        if isinstance(principal_raw, str)
        else tunnel_mod.parse_principal_from_env()
    )
    discover_wait_s = 1.5
    if isinstance(discover_wait_raw, str):
        try:
            discover_wait_s = float(discover_wait_raw)
        except ValueError:
            sys.stderr.write(
                f"holo tunnel up: invalid --discover-wait value "
                f"{discover_wait_raw!r}\n"
            )
            return 2

    record = tunnel_mod.find_cloudcity(instance, wait_s=discover_wait_s)
    if record is None:
        sys.stderr.write(
            f"holo tunnel up: no CloudCity matching {instance!r} found "
            f"on the LAN within {discover_wait_s}s. "
            f"Is `holo cloudcity announce` running on the host?\n"
        )
        return 1

    try:
        tunnel = tunnel_mod.open_to_cloudcity(
            record,
            backend=backend,
            key_path=key_path,
            principal=principal,
        )
    except cert_mod.CertFetchError as e:
        sys.stderr.write(f"holo tunnel up: cert fetch failed: {e}\n")
        return 1
    except tunnel_mod.TunnelError as e:
        sys.stderr.write(f"holo tunnel up: {e}\n")
        return 1
    except subprocess.CalledProcessError as e:
        stderr_excerpt = (e.stderr or b"").decode("utf-8", errors="replace")
        sys.stderr.write(
            f"holo tunnel up: ssh-keygen failed (exit {e.returncode}): "
            f"{stderr_excerpt}\n"
        )
        return 1

    target = tunnel.target
    info = {
        "tunnel_port": tunnel.port,
        "cloudcity_instance": record.get("instance"),
        "cloudcity_host": record.get("host"),
        "cloudcity_target": f"{target[0]}:{target[1]}" if target else None,
    }
    print(_json.dumps(info, indent=2))
    print("(Ctrl-C to tear down)", file=sys.stderr, flush=True)

    def _on_term(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)
    try:
        # Block on the ssh subprocess. If it dies on its own (e.g.
        # the CloudCity went away), we exit too — Phase 4b's reconnect
        # logic isn't in this PR.
        while tunnel.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.stop()
    return 0


COMMANDS = {
    "windows": _cmd_windows,
    "doctor": _cmd_doctor,
    "demo": _cmd_demo,
    "focus": _cmd_focus,
    "mcp": _cmd_mcp,
    "install-screen": _cmd_install_screen,
    "ide": _cmd_ide,
}


def _print_help() -> None:
    print(f"""holo {__version__} — browser + screen automation for AI agents

Usage: holo <command> [options]

Commands:
  doctor                  check macOS permissions / runtime environment
  demo [--manual] [--hide-qr] [--screen]
                          end-to-end smoke test against the in-page agent
  mcp [--listen PORT] [--hide-qr] [--screen] [--no-bookmarklet]
      [--no-browser] [--input-proxy HOST:PORT | --remote-input HOST:PORT]
      [--remote-screen HOST:PORT]
      [--announce] [--announce-session NAME] [--announce-user NAME]
      [--announce-ssh-user NAME] [--announce-ip A,B,C]
      [--announce-capabilities] [--announce-command "CMD"]
      [--auto-tunnel] [--auto-tunnel-backend URL]
                          run the MCP server over stdio (or TCP with --listen)
                          --screen          enable screen / template / app_activate tools
                          --no-bookmarklet  drop channel tools; no WS server
                          --no-browser      drop browser_* AppleScript tools
                                            (input-only peer in split topology)
                          --input-proxy HOST:PORT  route screen_click/screen_key/
                          (or --remote-input)        screen_type/screen_scroll/
                                            app_activate to a remote holo at
                                            HOST:PORT (use when corporate policy
                                            blocks local mouse/keyboard
                                            injection but a peer with a Screen
                                            Sharing client to this host can
                                            inject for you). Capture stays local
                                            unless --remote-screen is also set.
                          --remote-screen HOST:PORT  route screen_shot /
                                            screen_find_image / ui_template_*
                                            capture ops to a remote holo at
                                            HOST:PORT (use when this host can't
                                            do screen capture and a peer can —
                                            typically a peer mirroring this
                                            host's display via Screen Sharing
                                            so its capture IS your screen).
                                            When both proxies target the same
                                            host, holo shares one MCP
                                            connection.
                          --announce        broadcast session via mDNS
                          --announce-session NAME    logical session id
                          --announce-user NAME       display label (default: $USER)
                          --announce-ssh-user NAME   SSH login user
                          --announce-ip A,B,C        IPv4 override; each entry is a
                                                     literal IP or a trailing-dot
                                                     prefix (e.g. `192.168.1.`) that
                                                     filters the enumerated set
                          --announce-capabilities    serve hardware + applications +
                                                     packages inventory over a
                                                     token-auth HTTP endpoint
                                                     (everything auto-discovered;
                                                     no per-probe flags)
                          --announce-command "CMD"   shell command the desktop SPA
                                                     should run on the remote
                                                     after SSH connects.
                                                     Auto-defaults to the right
                                                     attach command when running
                                                     inside tmux ($TMUX) or
                                                     screen ($STY); override
                                                     with this flag for custom
                                                     multiplexers, login-shell
                                                     wrappers (e.g. for Homebrew
                                                     PATH), or REPLs. Published
                                                     verbatim in the announce.
                          --auto-tunnel              open reverse SSH forwards into
                                                     every discovered CloudCity
                                                     (one tunnel per CC); the
                                                     session announce gains a
                                                     tunnel_ports map (cc to
                                                     port) SPAs use to route
                          --auto-tunnel-backend URL  cert-signing backend for the
                                                     auto-tunnel cert refresh.
                                                     Defaults to per-CC announced
                                                     backend, then HOLO_BACKEND,
                                                     then https://api-dev.tai.sh
  connect HOST:PORT       stdio↔TCP bridge to a listening `holo mcp`
  mcp-remote -- CMD ...   spawn-per-connection stdio proxy
  discover [--json | --tail | --serve PORT] [--wait SECS]
           [--stale-after SECS] [--cors-origin O,O,...]
                          discover live `_holo-session._tcp.local.` broadcasts
                          (reference consumer for docs/companion-spec.md)
  cloudcity announce [--port PORT] [--ips A,B,...] [--backend URL]
                     [--ca-fps FP[,FP...]] [--instance NAME]
                          broadcast a `_cloudcity._tcp.local.` mDNS service
                          for the c2w-net Docker container's exposed sshd,
                          so holo daemons can reverse-tunnel into it (run on
                          the machine hosting c2w-net)
  cloudcity discover [--json | --tail] [--wait SECS] [--stale-after SECS]
                          subscribe to `_cloudcity._tcp.local.` broadcasts
                          and emit records (CLI mode only — HTTP/WS surface
                          lives inside `holo discover --serve PORT` as
                          `GET /cloudcities`)
  cert <show|get|refresh> [--backend URL] [--key-path PATH]
                          manage this holo daemon's outbound-SSH cert
                          (used by `holo tunnel up` to reach a CloudCity
                          host's sshd as `lando`); see `holo cert --help`
  tunnel up <cloudcity-instance> [--backend URL] [--key-path PATH]
            [--principal NAME] [--discover-wait SECS]
                          establish a reverse SSH tunnel to a CloudCity
                          host; see `holo tunnel --help`
  windows                 print visible windows (smoke for windows reader)
  screen <verb>           smoke-test the SikuliX-backed screen tools directly
  install-screen          pre-download the SikuliX jar into the user cache
  install-bookmarklet     download the bookmarklet page and open it
  install-remote HOST     SCP this machine's holo binary + cached
                          SikuliX jar to a peer Mac (user@host) over
                          SSH and install them at ~/bin/holo + the
                          standard cache. For air-gapped install of the
                          input-proxy peer. Requires `ssh HOST` to work.
  ide                     launch the SikuliX IDE (downloads the jar on
                          first use); requires `java` on PATH
  init <cli> [--force]    scaffold an MCP config file for the given CLI in
                          the current directory. Currently supports:
                          `claude` (writes .mcp.json). Prompts for
                          --announce-ip (auto-detected) and, when tmux is
                          on PATH, a tmux-attach --announce-command.
  upgrade                 check GitHub for a newer release and install it
                          in place at this binary's current path

Options:
  -h, --help              show this help and exit
  -V, --version           print version and exit

Quick start: `holo install-bookmarklet` to install the bookmarklet, then
`holo mcp` to run the MCP server.""")


_MISSING_ARG = object()


def _value_flag(rest: list[str], flag: str) -> str | None | object:
    """Read a `--flag VALUE` pair from `rest`.

    Returns:
        - ``None`` if the flag is absent
        - ``_MISSING_ARG`` if the flag is present but lacks a value
        - the string value otherwise
    """
    if flag not in rest:
        return None
    i = rest.index(flag)
    if i + 1 >= len(rest):
        return _MISSING_ARG
    value = rest[i + 1]
    if value.startswith("--"):
        return _MISSING_ARG
    return value


def _print_update_notice_if_any() -> None:
    """Append a one-line `Update available: …` notice after the help
    screen when a newer release is on GitHub. Silent on cache miss,
    network failure, or dev install — the help command must always
    succeed.
    """
    try:
        from holo import upgrade
        latest = upgrade.check_for_update(timeout=1.0)
    except Exception:  # noqa: BLE001 - help must never fail
        return
    if latest:
        print()
        print(f"Update available: {latest} — run `holo upgrade`")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        _print_help()
        _print_update_notice_if_any()
        return 0
    cmd = args[0]
    rest = args[1:]
    if cmd in {"-h", "--help", "help"}:
        _print_help()
        return 0
    if cmd in {"-V", "--version"}:
        print(__version__)
        return 0
    if cmd == "demo":
        return _cmd_demo(
            manual="--manual" in rest,
            hide_qr="--hide-qr" in rest,
            enable_screen="--screen" in rest,
        )
    if cmd == "mcp":
        listen_port: int | None = None
        if "--listen" in rest:
            i = rest.index("--listen")
            if i + 1 >= len(rest):
                sys.stderr.write("holo mcp: --listen requires a port number\n")
                return 2
            try:
                listen_port = int(rest[i + 1])
            except ValueError:
                sys.stderr.write(
                    f"holo mcp: invalid --listen port {rest[i + 1]!r}\n"
                )
                return 2
            if not (0 < listen_port < 65536):
                sys.stderr.write(
                    f"holo mcp: --listen port {listen_port} out of range\n"
                )
                return 2

        announce_session = _value_flag(rest, "--announce-session")
        announce_user = _value_flag(rest, "--announce-user")
        announce_ssh_user = _value_flag(rest, "--announce-ssh-user")
        announce_ip_raw = _value_flag(rest, "--announce-ip")
        announce_command_raw = _value_flag(rest, "--announce-command")
        announce = "--announce" in rest
        announce_capabilities = "--announce-capabilities" in rest
        auto_tunnel = "--auto-tunnel" in rest
        auto_tunnel_backend_raw = _value_flag(rest, "--auto-tunnel-backend")

        # `--probe-software` and `--probe-pkg` were removed when
        # capabilities went auto. Catch old commands so users see a
        # useful error instead of "unrecognised flag".
        if "--probe-software" in rest or "--probe-pkg" in rest:
            sys.stderr.write(
                "holo mcp: --probe-software / --probe-pkg were removed in "
                "0.1.0a16; software (applications + packages) is now "
                "auto-discovered per platform when --announce-capabilities "
                "is set. Drop the flag and re-run.\n"
            )
            return 2

        if not announce and (
            announce_session is not None
            or announce_user is not None
            or announce_ssh_user is not None
            or announce_ip_raw is not None
            or announce_capabilities
            or announce_command_raw is not None
            or auto_tunnel
            or auto_tunnel_backend_raw is not None
        ):
            sys.stderr.write(
                "holo mcp: --announce-session/--announce-user/"
                "--announce-ssh-user/--announce-ip/--announce-capabilities/"
                "--announce-command/--auto-tunnel/--auto-tunnel-backend "
                "require --announce\n"
            )
            return 2
        if announce_session is _MISSING_ARG:
            sys.stderr.write("holo mcp: --announce-session requires a value\n")
            return 2
        if announce_user is _MISSING_ARG:
            sys.stderr.write("holo mcp: --announce-user requires a value\n")
            return 2
        if announce_ssh_user is _MISSING_ARG:
            sys.stderr.write("holo mcp: --announce-ssh-user requires a value\n")
            return 2
        if announce_ip_raw is _MISSING_ARG:
            sys.stderr.write("holo mcp: --announce-ip requires a value\n")
            return 2
        if announce_command_raw is _MISSING_ARG:
            sys.stderr.write(
                "holo mcp: --announce-command requires a value\n"
            )
            return 2
        if auto_tunnel_backend_raw is _MISSING_ARG:
            sys.stderr.write(
                "holo mcp: --auto-tunnel-backend requires a value\n"
            )
            return 2

        announce_ips: list[str] | None = None
        if isinstance(announce_ip_raw, str):
            announce_ips = [
                ip.strip() for ip in announce_ip_raw.split(",") if ip.strip()
            ]
            if not announce_ips:
                sys.stderr.write(
                    "holo mcp: --announce-ip requires at least one IP\n"
                )
                return 2

        auto_tunnel_backend = (
            auto_tunnel_backend_raw
            if isinstance(auto_tunnel_backend_raw, str)
            else None
        )
        announce_command = (
            announce_command_raw
            if isinstance(announce_command_raw, str)
            else None
        )

        def _parse_endpoint_flag(name: str) -> tuple[str, int] | None | int:
            """Return the parsed (host, port), None if absent, or an int
            exit code (2) on validation error (caller propagates)."""
            raw = _value_flag(rest, name)
            if raw is _MISSING_ARG:
                sys.stderr.write(f"holo mcp: {name} requires a HOST:PORT value\n")
                return 2
            if not isinstance(raw, str):
                return None
            if ":" not in raw:
                sys.stderr.write(f"holo mcp: {name} {raw!r} must be HOST:PORT\n")
                return 2
            host, _, port_s = raw.rpartition(":")
            try:
                port = int(port_s)
            except ValueError:
                sys.stderr.write(f"holo mcp: {name} port {port_s!r} not an integer\n")
                return 2
            if not (0 < port < 65536):
                sys.stderr.write(f"holo mcp: {name} port {port} out of range\n")
                return 2
            if not host:
                sys.stderr.write(f"holo mcp: {name} host is empty\n")
                return 2
            return (host, port)

        # --input-proxy is the original flag; --remote-input is the alias
        # that lines up with --remote-screen. Accept either, reject both
        # together (since they'd compete for the same kwarg).
        input_proxy: tuple[str, int] | None = None
        for flag in ("--input-proxy", "--remote-input"):
            parsed = _parse_endpoint_flag(flag)
            if parsed == 2:
                return 2
            if parsed is not None:
                if input_proxy is not None:
                    sys.stderr.write(
                        "holo mcp: --input-proxy and --remote-input are "
                        "aliases — pass only one\n"
                    )
                    return 2
                input_proxy = parsed  # type: ignore[assignment]

        remote_screen: tuple[str, int] | None = None
        rs_parsed = _parse_endpoint_flag("--remote-screen")
        if rs_parsed == 2:
            return 2
        if rs_parsed is not None:
            remote_screen = rs_parsed  # type: ignore[assignment]

        return _cmd_mcp(
            hide_qr="--hide-qr" in rest,
            enable_screen="--screen" in rest,
            no_bookmarklet="--no-bookmarklet" in rest,
            no_browser="--no-browser" in rest,
            input_proxy=input_proxy,
            remote_screen=remote_screen,
            listen_port=listen_port,
            announce=announce,
            announce_session=announce_session,
            announce_user=announce_user,
            announce_ssh_user=announce_ssh_user,
            announce_ips=announce_ips,
            announce_capabilities=announce_capabilities,
            announce_command=announce_command,
            auto_tunnel=auto_tunnel,
            auto_tunnel_backend=auto_tunnel_backend,
        )
    if cmd == "connect":
        return _cmd_connect(rest)
    if cmd == "mcp-remote":
        return _cmd_mcp_remote(rest)
    if cmd == "screen":
        return _cmd_screen(rest)
    if cmd == "install-bookmarklet":
        return _cmd_install_bookmarklet(rest)
    if cmd == "discover":
        return _cmd_discover(rest)
    if cmd == "cloudcity":
        return _cmd_cloudcity(rest)
    if cmd == "cert":
        return _cmd_cert(rest)
    if cmd == "tunnel":
        return _cmd_tunnel(rest)
    if cmd == "init":
        if not rest:
            sys.stderr.write(
                "holo init: missing CLI argument (e.g. `holo init claude`)\n"
            )
            return 2
        from holo import init_config
        return init_config.run(rest[0], force="--force" in rest[1:])
    if cmd == "install-remote":
        if not rest:
            sys.stderr.write(
                "holo install-remote: missing HOST "
                "(e.g. `holo install-remote user@machine-b`)\n"
            )
            return 2
        from holo import install_remote
        return install_remote.run(rest[0])
    if cmd == "upgrade":
        from holo import upgrade as upgrade_mod
        return upgrade_mod.run_upgrade(force="--force" in rest)
    if cmd in COMMANDS:
        return COMMANDS[cmd]()
    print(
        f"holo: unknown command {cmd!r} (try `holo --help`)", file=sys.stderr
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
