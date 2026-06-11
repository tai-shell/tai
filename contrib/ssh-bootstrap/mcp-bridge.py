#!/usr/bin/env python3
"""mcp-bridge.py — stdio↔SSH-tunneled-TCP MCP relay.

This is what Claude spawns as its MCP server command for a remote
tai. It:

  1. SSHes to the target and reads the per-session token from
     /tmp/tai-token (path overridable).
  2. Opens an SSH -L tunnel so 127.0.0.1:PORT on the local host
     reaches 127.0.0.1:PORT on the remote (where tcsh is listening).
  3. Connects to localhost:PORT, sends `TAI/1\\n<token>\\n`.
  4. Waits for `OK\\n` from the server.
  5. Relays Claude's stdio ↔ socket bidirectionally for the rest
     of the session.

The SSH tunnel goes the "normal" direction (A → B). The token
fetched in step 1 is what authenticates the connection to a tcsh
listener that B itself started inside Terminal.app — so the bridge
spawned by sshd never participates in TCC; tcsh's permissions
come from Terminal.app (its responsible process at exec time).

Usage:    mcp-bridge.py user@hostB PORT [/path/to/remote-token]

Exit codes:
    0   client (Claude) closed the channel cleanly
    1   handshake / token / tunnel error  (diagnostic on stderr)
   64   missing or bad argv
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time

DEFAULT_TOKEN_PATH = "/tmp/tai-token"
TUNNEL_READY_TIMEOUT_S = 10.0
HANDSHAKE_TIMEOUT_S = 10.0


def die(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"mcp-bridge: {msg}\n")
    sys.exit(code)


def fetch_token(target: str, remote_path: str) -> str:
    # CRITICAL: stdin=DEVNULL. subprocess.run defaults stdin to
    # INHERIT from the parent — meaning ssh would read from
    # mcp-bridge.py's stdin (the pipe carrying Claude's MCP
    # frames). ssh appears to consume bytes from stdin even when
    # the remote command (`cat /tmp/tai-token`) doesn't read its
    # own stdin. Without DEVNULL here, the very first ssh in
    # main() silently eats whatever was already buffered in the
    # pipe and Claude's frames are lost before relay() ever runs.
    # Symptom: relay sees `stdin EOF` immediately and stdout is
    # empty. Took an embarrassingly long time to find.
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target, "cat", remote_path],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        die(f"fetching {remote_path} from {target}: "
            f"{(proc.stderr or 'ssh failed').strip()}")
    token = proc.stdout.strip()
    if not token:
        die(f"{remote_path} on {target} is empty")
    return token


def open_tunnel(target: str, port: int) -> subprocess.Popen:
    """Open `ssh -L PORT:127.0.0.1:PORT TARGET` and return the Popen.

    CRITICAL: do NOT probe the local forwarded port by opening test
    TCP connects. Each successful test-connect is forwarded through
    the tunnel to tcsh's listener — and `tcsh --listen` is single-
    connection. The probe IS the session: tcsh accepts the test
    connect, the probe immediately closes the socket without sending
    anything, tcsh reads an empty line, logs 'bad magic prefix' to
    its log, and exits. By the time the real handshake-connect runs,
    tcsh is gone → ConnectionResetError.

    Instead, trust ExitOnForwardFailure=yes plus a short settle
    window. If ssh is still alive after settling, the forward is up
    (in practice ssh binds the local port within ~100 ms on
    loopback). If the forward can't bind (port in use, etc.), ssh
    exits and we read the error from its captured stderr.
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
            return proc        # still alive → forward is up
        err = (proc.stderr.read() or b"").decode(errors="replace").strip()
        die(f"ssh tunnel exited early (rc={rc}): "
            f"{err or '(no stderr captured)'}")
    proc.kill()
    die(f"ssh tunnel didn't settle within {TUNNEL_READY_TIMEOUT_S}s")


def handshake(sock: socket.socket, token: str) -> bytes:
    """Send `TAI/1\\n<token>\\n`, wait for `OK\\n`. Returns any bytes
    that arrived after the `OK\\n` (which already belong to the MCP
    stream and need to be forwarded immediately).

    Uses blocking I/O — NO settimeout(). Empirically, the
    settimeout(X) + settimeout(None) dance broke the subsequent
    relay's daemon-thread `recv()` on at least one macOS box: the
    socket appeared to stay in a degraded mode where recv blocked
    but never returned data, even though the inline-equivalent code
    without the settimeout calls worked fine end-to-end. This is
    likely a Python/macOS subtlety around how `settimeout(None)`
    interacts with non-blocking mode internally. The handshake is
    fast enough that a separate timeout isn't worth the risk —
    fail-on-server-close is enough."""
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


def relay(sock: socket.socket, primer: bytes) -> None:
    """Bidirectional pump between Claude's stdio and the socket. The
    server's first bytes of MCP framing may have already arrived
    during the handshake recv — `primer` holds them and gets
    flushed before the pump starts.

    Structure: one daemon thread reads sock → stdout. Foreground
    thread reads stdin → sock. When stdin EOFs, foreground shuts
    down the WR half of the socket (so the server sees EOF on its
    stdin side and finishes its MCP loop). Foreground then JOINS
    the reader so we don't kill it mid-recv before all responses
    have drained.

    Why this matters: an earlier attempt used a `stop` event and a
    100ms main-poll. When stdin EOFed, the event fired, main exited,
    and the daemon reader was killed before draining the server's
    responses — so stdout ended up empty even though the server had
    sent kilobytes of MCP. The bug only showed up at session-end;
    during the session it looked like everything was working.
    """
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
        # Tell the server we're done writing — it sees EOF on its
        # stdin and finishes processing any pending requests.
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    # Give the reader time to drain the server's final responses +
    # see the server close its end. Bounded so we don't hang if the
    # server never closes (shouldn't happen, but defensively).
    reader.join(timeout=30.0)


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
    remote_token_path = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_TOKEN_PATH

    sys.stderr.write(
        f"mcp-bridge: fetching token from {target}:{remote_token_path}\n")
    token = fetch_token(target, remote_token_path)

    sys.stderr.write(
        f"mcp-bridge: opening tunnel localhost:{port} → {target}:{port}\n")
    tunnel = open_tunnel(target, port)

    # Make sure tunnel dies with us, even on SIGTERM from Claude.
    def cleanup(*_: object) -> None:
        try:
            tunnel.terminate()
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    try:
        sock = socket.socket()
        sock.connect(("127.0.0.1", port))

        sys.stderr.write("mcp-bridge: handshake (TAI/1 + token)\n")
        primer = handshake(sock, token)
        sys.stderr.write("mcp-bridge: ready, relaying MCP\n")
        relay(sock, primer)
    finally:
        try:
            tunnel.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
