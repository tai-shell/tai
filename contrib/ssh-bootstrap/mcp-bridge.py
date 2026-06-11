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
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target, "cat", remote_path],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        die(f"fetching {remote_path} from {target}: "
            f"{(proc.stderr or 'ssh failed').strip()}")
    token = proc.stdout.strip()
    if not token:
        die(f"{remote_path} on {target} is empty")
    return token


def open_tunnel(target: str, port: int) -> subprocess.Popen:
    # -N: no remote command, just forward. -T: no TTY. -o
    # ExitOnForwardFailure: bail if the port can't bind.
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
    deadline = time.time() + TUNNEL_READY_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode(errors="replace").strip()
            die(f"ssh tunnel exited early: {err or '(no stderr)'}")
        try:
            with socket.socket() as s:
                s.settimeout(0.3)
                s.connect(("127.0.0.1", port))
            return proc
        except OSError:
            time.sleep(0.2)
    proc.kill()
    die("ssh tunnel didn't open within "
        f"{TUNNEL_READY_TIMEOUT_S}s")


def handshake(sock: socket.socket, token: str) -> bytes:
    """Send `TAI/1\\n<token>\\n`, wait for `OK\\n`. Returns any bytes
    that arrived after the `OK\\n` (which already belong to the MCP
    stream and need to be forwarded immediately)."""
    sock.sendall(f"TAI/1\n{token}\n".encode())
    sock.settimeout(HANDSHAKE_TIMEOUT_S)
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(64)
        if not chunk:
            die("server closed during handshake (token mismatch?)")
        buf += chunk
    line, rest = buf.split(b"\n", 1)
    if line != b"OK":
        die(f"handshake rejected: {line.decode(errors='replace')}")
    sock.settimeout(None)
    return rest


def relay(sock: socket.socket, primer: bytes) -> None:
    """Bidirectional pump between Claude's stdio and the socket. The
    server's first bytes of MCP framing may have already been read
    during the handshake search — `primer` holds them and gets
    flushed before the pump starts."""
    if primer:
        sys.stdout.buffer.write(primer)
        sys.stdout.buffer.flush()

    stop = threading.Event()

    def server_to_stdout() -> None:
        try:
            while not stop.is_set():
                data = sock.recv(4096)
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        finally:
            stop.set()

    def stdin_to_server() -> None:
        try:
            while not stop.is_set():
                data = sys.stdin.buffer.read1(4096)
                if not data:
                    break
                sock.sendall(data)
        finally:
            stop.set()
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t1 = threading.Thread(target=server_to_stdout, daemon=True)
    t2 = threading.Thread(target=stdin_to_server, daemon=True)
    t1.start(); t2.start()
    while not stop.is_set():
        time.sleep(0.1)


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
