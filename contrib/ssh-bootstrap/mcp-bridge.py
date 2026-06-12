#!/usr/bin/env python3
"""mcp-bridge.py — agentless MCP bridge from Host A to tai on Host B.

This is what Claude spawns as its MCP server command. It does the
entire setup transparently:

  1. Ensures /tmp/tai on Host B matches our local TAI_BINARY (scp's
     it over only if missing or sha mismatches — typically free
     after first session).
  2. Generates a one-shot launcher.command with a fresh per-session
     token, scps it, and `open`s it via SSH so it runs inside
     Terminal.app on B. The launcher starts a detached
     `tcsh --listen PORT --token T` listener under Terminal.app's
     TCC scope.
  3. Waits (via SSH pgrep, NOT TCP probe — that would consume the
     single-connection listener) until tcsh is bound.
  4. Opens an SSH `-L PORT:127.0.0.1:PORT` tunnel.
  5. Connects, sends `TAI/1\\n<token>\\n` handshake, waits for `OK\\n`.
  6. Relays Claude's stdio ↔ socket bidirectionally.

The user never runs bootstrap.sh — Claude's MCP-server-spawn IS the
bootstrap. `contrib/ssh-bootstrap/bootstrap.sh` stays around as a
debug / manual-pre-stage tool but isn't required for normal use.

Usage:    mcp-bridge.py user@hostB PORT [/path/to/remote-token]

Env:
    TAI_BINARY     local path to tai-bundled-macos-universal2
                   (default: /usr/local/bin/tai). Must exist + be
                   executable; sha256 is compared against /tmp/tai
                   on the remote each session.

Exit codes:
    0   client (Claude) closed the channel cleanly
    1   bootstrap / handshake / tunnel error  (diagnostic on stderr)
   64   missing or bad argv
"""
from __future__ import annotations

import hashlib
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

DEFAULT_TOKEN_PATH = "/tmp/tai-token"
DEFAULT_TAI_BINARY = "/usr/local/bin/tai"
DEFAULT_REMOTE_BINARY_PATH = "/tmp/tai"
DEFAULT_REMOTE_TCSH_LINK = "/tmp/tcsh"
DEFAULT_REMOTE_LAUNCHER_PATH = "/tmp/launcher.command"
DEFAULT_REMOTE_LOG_PATH = "/tmp/tai-listener.log"
DEFAULT_REMOTE_CACHE_DIR = "~/Library/Caches/tai"

TUNNEL_READY_TIMEOUT_S = 10.0
LISTENER_READY_TIMEOUT_S = 10.0


def die(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"mcp-bridge: {msg}\n")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Step 1 — ensure the remote binary matches our local one
# ---------------------------------------------------------------------------


def _ssh(target: str, remote_cmd: str, *, timeout: float = 15.0,
         check: bool = False) -> subprocess.CompletedProcess:
    """Run `ssh -o BatchMode=yes target <cmd>` with stdin closed so
    we never accidentally eat the parent's stdin pipe."""
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target, remote_cmd],
        capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL, check=check,
    )


def _scp(local: str, remote: str, *, timeout: float = 600.0) -> None:
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", "-q", local, remote],
        stdin=subprocess.DEVNULL, timeout=timeout, check=True,
    )


def _sh_squote(s: str) -> str:
    """Single-quote a string for safe interpolation into a sh -c
    command. Single quotes inside the string are handled by closing,
    inserting an escaped quote, and reopening."""
    return "'" + s.replace("'", "'\\''") + "'"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_binary(target: str, local_path: str) -> None:
    if not os.path.isfile(local_path):
        die(f"TAI_BINARY={local_path} not found")
    if not os.access(local_path, os.X_OK):
        die(f"TAI_BINARY={local_path} not executable (run `chmod +x`)")

    sys.stderr.write(
        f"mcp-bridge: checking {target}:{DEFAULT_REMOTE_BINARY_PATH}\n")
    r = _ssh(target, f"shasum -a 256 {DEFAULT_REMOTE_BINARY_PATH} 2>/dev/null "
                     f"| awk '{{print $1}}'")
    remote_sha = r.stdout.strip()
    local_sha = _sha256(local_path)

    if remote_sha == local_sha:
        sys.stderr.write(
            "mcp-bridge: remote binary already up to date\n")
        return

    sys.stderr.write(
        f"mcp-bridge: shipping binary "
        f"({os.path.getsize(local_path) / 1_048_576:.0f} MB) → "
        f"{target}:{DEFAULT_REMOTE_BINARY_PATH}\n")
    _scp(local_path, f"{target}:{DEFAULT_REMOTE_BINARY_PATH}")
    _ssh(target,
         f"chmod +x {DEFAULT_REMOTE_BINARY_PATH} && "
         f"xattr -d com.apple.quarantine {DEFAULT_REMOTE_BINARY_PATH} "
         f"2>/dev/null || true",
         check=True)


# ---------------------------------------------------------------------------
# Step 2 — generate launcher.command with fresh token, scp it, fire it
# ---------------------------------------------------------------------------


def fire_listener(target: str, port: int) -> str:
    """Ship a freshly-templated launcher.command and fire it through
    `open` so it runs inside Terminal.app. Returns the token baked
    into this launch."""
    token = secrets.token_hex(32)

    launcher = textwrap.dedent(f"""\
        #!/bin/bash
        # Auto-generated by mcp-bridge.py per Claude session — DO NOT EDIT.
        # Runs inside Terminal.app on Host B so the spawned tcsh inherits
        # Terminal.app's TCC permissions (Accessibility / Screen Recording).
        set -eu

        # Bounds-snap + Terminal-hide are done by the SSH-side
        # osascript that fired this script (single AppleScript
        # block right after `do script`), so by the time we start
        # running, the window's already offscreen and Terminal is
        # invisible. Nothing to do here on that front.

        ln -sf {DEFAULT_REMOTE_BINARY_PATH} /tmp/tcsh
        printf '%s\\n' '{token}' > {DEFAULT_TOKEN_PATH}
        chmod 600 {DEFAULT_TOKEN_PATH}

        # Kill any prior listener (single-connection design).
        pkill -f '^/tmp/tcsh --listen' >/dev/null 2>&1 || true

        # Detached listener — nohup + disown so the Terminal window
        # can close while it keeps running. TCC responsibility is
        # set at exec() time so it stays Terminal.app even after
        # this parent bash exits and tcsh is reparented to launchd.
        nohup /tmp/tcsh --listen {port} --token '{token}' \\
            < /dev/null > {DEFAULT_REMOTE_LOG_PATH} 2>&1 &
        disown

        # Auto-close the (offscreen) Terminal window after a beat.
        # Background so it fires after this script's exit.
        ( sleep 1 && \\
          osascript -e 'tell application "Terminal" to close (every window whose name contains "launcher")' \\
            >/dev/null 2>&1 ) &
        exit 0
        """)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".command", delete=False,
    ) as tmp:
        tmp.write(launcher)
        local_launcher = tmp.name

    try:
        os.chmod(local_launcher, 0o755)
        sys.stderr.write(
            f"mcp-bridge: shipping launcher → "
            f"{target}:{DEFAULT_REMOTE_LAUNCHER_PATH}\n")
        _scp(local_launcher, f"{target}:{DEFAULT_REMOTE_LAUNCHER_PATH}",
             timeout=60.0)
        sys.stderr.write(
            "mcp-bridge: firing launcher inside Terminal.app on remote\n")
        # Use osascript directly (not `open`) so the bounds-snap runs
        # as the very next AppleScript statement after `do script`,
        # with no `bash → osascript` startup gap in between. Then
        # hide Terminal entirely via System Events so any stray
        # window can't steal focus or remain on screen.
        applescript_lines = [
            'tell application "Terminal"',
            f'  do script "{DEFAULT_REMOTE_LAUNCHER_PATH}"',
            '  set bounds of front window to {-10000, -10000, -9900, -9900}',
            'end tell',
            'tell application "System Events"',
            '  set visible of process "Terminal" to false',
            'end tell',
        ]
        # Each AppleScript line as a separate -e arg. Single-quote
        # each in the shell command; the script's literal { and }
        # bounds-tuple braces are safe inside the single quotes.
        ascript = " ".join(
            "-e " + _sh_squote(line) for line in applescript_lines
        )
        _ssh(target,
             f"chmod +x {DEFAULT_REMOTE_LAUNCHER_PATH} && osascript {ascript}",
             check=True)
    finally:
        os.unlink(local_launcher)

    return token


# ---------------------------------------------------------------------------
# Step 3 — wait for tcsh to bind. Check via SSH pgrep so we don't
# consume the single-connection listener with a TCP probe.
# ---------------------------------------------------------------------------


def wait_for_listener(target: str) -> None:
    """Poll the listener log on the remote for tcsh's bind banner.

    Why not pgrep: pgrep returns success the moment tcsh's process
    exec's, which is BEFORE the socket() → bind() → listen() chain
    completes. A connection arriving during that window hits a
    tcsh that's running but not yet in accept(), which manifests
    as the bridge's sock.connect succeeding (ssh tunnel forwards
    fine) but recv hitting RST a millisecond later when tcsh's
    accept queue isn't there. Empirically observed against macOS
    26 when the binary scp window pushes the timing into the bad
    zone; a 3-second sleep after `open` was enough to dodge it.

    The log banner ("tai: tcsh listening on 127.0.0.1:PORT ...")
    is printed by tcsh's C code immediately after listen() returns
    success — so its presence in the log is proof tcsh is ready
    to accept. Cheap to check via ssh + grep.
    """
    sys.stderr.write("mcp-bridge: waiting for tcsh listener to bind\n")
    deadline = time.time() + LISTENER_READY_TIMEOUT_S
    while time.time() < deadline:
        r = _ssh(
            target,
            f"grep -q 'tcsh listening on' {DEFAULT_REMOTE_LOG_PATH} "
            f"2>/dev/null && echo ready",
            timeout=5.0,
        )
        if r.returncode == 0 and "ready" in r.stdout:
            return
        time.sleep(0.3)
    die(f"listener didn't appear on remote within "
        f"{LISTENER_READY_TIMEOUT_S}s "
        f"(check /tmp/tai-listener.log on the remote for errors)")


# ---------------------------------------------------------------------------
# Step 4 — open SSH -L tunnel. Do NOT probe via TCP connect.
# ---------------------------------------------------------------------------


def open_tunnel(target: str, port: int) -> subprocess.Popen:
    """Open `ssh -L PORT:127.0.0.1:PORT TARGET` and return the Popen.

    Do NOT probe the local forwarded port by opening test TCP
    connects. Each successful test-connect is forwarded through the
    tunnel to tcsh's listener — and `tcsh --listen` is single-
    connection. The probe IS the session: tcsh accepts the test,
    the probe immediately closes, tcsh reads an empty line, logs
    'bad magic prefix', and exits. By the time the real handshake-
    connect runs, tcsh is gone → ConnectionResetError.

    Instead, trust ExitOnForwardFailure=yes + a short settle. If
    ssh is still alive after settling, the forward is up.
    """
    proc = subprocess.Popen(
        ["ssh", "-N", "-T",
         "-o", "ExitOnForwardFailure=yes",
         "-o", "ServerAliveInterval=30",
         "-o", "BatchMode=yes",
         "-L", f"{port}:127.0.0.1:{port}",
         target],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    settle = 0.5
    elapsed = 0.0
    while elapsed < TUNNEL_READY_TIMEOUT_S:
        time.sleep(settle)
        elapsed += settle
        rc = proc.poll()
        if rc is None:
            return proc
        err = (proc.stderr.read() or b"").decode(errors="replace").strip()
        die(f"ssh tunnel exited early (rc={rc}): "
            f"{err or '(no stderr captured)'}")
    proc.kill()
    die(f"ssh tunnel didn't settle within {TUNNEL_READY_TIMEOUT_S}s")


# ---------------------------------------------------------------------------
# Step 5 — handshake. Uses blocking I/O (no settimeout dance, which
# empirically degrades subsequent recv on this socket).
# ---------------------------------------------------------------------------


def handshake(sock: socket.socket, token: str) -> bytes:
    sock.sendall(f"TAI/1\n{token}\n".encode())
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(64)
        if not chunk:
            die("server closed during handshake (token mismatch?)")
        buf += chunk
    line, rest = buf.split(b"\n", 1)
    if line != b"OK":
        die(f"handshake rejected: {line.decode(errors='replace')}")
    return rest


# ---------------------------------------------------------------------------
# Step 6 — bidirectional stdio ↔ socket relay
# ---------------------------------------------------------------------------


def relay(sock: socket.socket, primer: bytes) -> None:
    """Foreground stdin pump + one daemon reader.

    When stdin EOFs, foreground shuts down SHUT_WR (so the server
    sees EOF on its stdin and finishes its MCP loop), then JOINs
    the reader so we don't kill it mid-recv before the final
    responses have drained."""
    if primer:
        sys.stdout.buffer.write(primer)
        sys.stdout.buffer.flush()

    def server_to_stdout() -> None:
        while True:
            try:
                data = sock.recv(4096)
            except OSError:
                return
            if not data:
                return
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    reader = threading.Thread(target=server_to_stdout, daemon=True)
    reader.start()

    try:
        while True:
            data = sys.stdin.buffer.read1(4096)
            if not data:
                break
            sock.sendall(data)
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    reader.join(timeout=30.0)


# ---------------------------------------------------------------------------
# Cleanup — wipe every trace of tai from the remote after the session.
# ---------------------------------------------------------------------------


def cleanup_remote(target: str) -> None:
    """True agentless: remove the binary, launcher, token, log, the
    /tmp/tcsh symlink, and the entire ~/Library/Caches/tai/ tree
    (extracted bundle payloads + bridge.log + template cache).

    Runs in main()'s finally so it fires even on abnormal exit. Best
    effort: a network drop or SSH error during cleanup is logged
    but not fatal — leftover bytes on the remote are not worth
    failing the session over.

    The single-connection listener is *supposed* to exit when the
    client disconnects, but on a SIGTERM/SIGINT exit that teardown
    races the tunnel close and the listener (plus the SikuliX JVM it
    spawned) can survive — observed holding /tmp/tcsh and the cache
    dir open after a CLI exit. So we pkill both before removing files
    rather than assuming they're already gone. Unix rm wouldn't care
    about open fds, but a live listener would keep re-touching the
    log and leave a process running on the remote.

    Set MCP_BRIDGE_KEEP_TRACES=1 in env to skip cleanup for
    debugging (preserve logs etc. on the remote for inspection)."""
    if os.environ.get("MCP_BRIDGE_KEEP_TRACES") == "1":
        sys.stderr.write(
            "mcp-bridge: cleanup skipped (MCP_BRIDGE_KEEP_TRACES=1)\n")
        return

    sys.stderr.write(f"mcp-bridge: wiping {target} (agentless cleanup)\n")
    # Single shell command so we only pay one ssh round-trip. Kill the
    # listener + any SikuliX JVM first (best-effort; pkill is fine if
    # they already exited), then remove files. `rm`s tolerate missing
    # files; the rm -rf of the cache dir cleans up any extracted-payload
    # subdirs in one shot.
    cmd = (
        f"pkill -f '^{DEFAULT_REMOTE_TCSH_LINK} --listen' 2>/dev/null; "
        f"pkill -f sikulixapi 2>/dev/null; "
        f"rm -f {DEFAULT_REMOTE_BINARY_PATH} {DEFAULT_REMOTE_TCSH_LINK} "
        f"{DEFAULT_REMOTE_LAUNCHER_PATH} {DEFAULT_TOKEN_PATH} "
        f"{DEFAULT_REMOTE_LOG_PATH}; "
        f"rm -rf {DEFAULT_REMOTE_CACHE_DIR}"
    )
    try:
        r = _ssh(target, cmd, timeout=30.0)
        if r.returncode != 0:
            sys.stderr.write(
                f"mcp-bridge: cleanup partial (rc={r.returncode}): "
                f"{(r.stderr or '').strip()}\n")
        else:
            sys.stderr.write("mcp-bridge: remote wiped clean\n")
    except (subprocess.TimeoutExpired, OSError) as e:
        sys.stderr.write(f"mcp-bridge: cleanup ssh failed: {e}\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 3:
        die("usage: mcp-bridge.py user@host PORT [remote-token-path]", 64)
    target, port_s = sys.argv[1], sys.argv[2]
    try:
        port = int(port_s)
    except ValueError:
        die(f"port must be an integer, got {port_s!r}", 64)
    if port < 1 or port > 65535:
        die(f"port out of range: {port}", 64)

    tai_binary = os.environ.get("TAI_BINARY", DEFAULT_TAI_BINARY)

    # Steps 1–3: ensure the remote is ready.
    ensure_binary(target, tai_binary)
    token = fire_listener(target, port)
    wait_for_listener(target)

    # Step 4: tunnel.
    tunnel = open_tunnel(target, port)

    # Teardown is shared between the signal handler and the `finally`
    # below; the flag makes it run exactly once. The previous handler
    # called os._exit(0) directly, which skipped the `finally` and so
    # never ran cleanup_remote — and Claude Code stops the MCP server
    # with SIGTERM, not a clean stdin EOF, so that was the common path.
    # Result: the remote binary/launcher/token/log + extracted cache and
    # a still-live listener were orphaned on every normal exit.
    teardown_done = {"yes": False}

    def teardown() -> None:
        if teardown_done["yes"]:
            return
        teardown_done["yes"] = True
        try:
            tunnel.terminate()
        except Exception:
            pass
        # Wipe every trace of tai from the remote so leftover bytes can't
        # accumulate across sessions / failed runs.
        cleanup_remote(target)

    def on_signal(*_: object) -> None:
        teardown()
        os._exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        # Step 5: handshake.
        sock = socket.socket()
        sock.connect(("127.0.0.1", port))
        sys.stderr.write("mcp-bridge: handshake (TAI/1 + token)\n")
        primer = handshake(sock, token)
        sys.stderr.write("mcp-bridge: ready, relaying MCP\n")
        # Step 6: relay.
        relay(sock, primer)
    finally:
        # Step 7 — runs on clean EOF exit; the signal handler covers the
        # SIGTERM/SIGINT paths. The shared flag prevents a double wipe.
        teardown()


if __name__ == "__main__":
    main()
