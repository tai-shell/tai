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
  3. Waits (via a single SSH that polls the listener log remote-side,
     NOT a TCP probe — a probe would burn a handshake slot) until
     tcsh is bound.
  4. Opens an SSH `-L PORT:127.0.0.1:PORT` tunnel.
  5. Connects, sends `TAI/1\\n<token>\\n` handshake, waits for `OK\\n`.
  6. Relays Claude's stdio ↔ socket bidirectionally, exiting non-zero
     the moment the socket or the tunnel dies (so Claude Code sees a
     failed server instead of a silently wedged one).

The user never runs bootstrap.sh — Claude's MCP-server-spawn IS the
bootstrap. `contrib/ssh-bootstrap/bootstrap.sh` stays around as a
debug / manual-pre-stage tool but isn't required for normal use.

Usage:    mcp-bridge.py user@hostB PORT [/path/to/remote-token]

Env:
    TAI_BINARY     local path to tai-bundled-macos-universal2
                   (default: /usr/local/bin/tai), OR an http(s) URL
                   to a release asset. A local path must exist + be
                   executable. A URL is downloaded once and cached
                   under ~/Library/Caches/tai/downloads/ keyed by the
                   asset's sha256 (fetched from a `<url>.sha256`
                   companion when present), so later sessions reuse the
                   cached binary with no re-download. Either way the
                   resolved binary's sha256 is compared against /tmp/tai
                   on the remote each session and scp'd only on mismatch.

Exit codes:
    0   client (Claude) closed the channel cleanly
    1   bootstrap / handshake / tunnel error  (diagnostic on stderr)
   64   missing or bad argv
"""
from __future__ import annotations

import hashlib
import os
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request

DEFAULT_TOKEN_PATH = "/tmp/tai-token"
DEFAULT_TAI_BINARY = "/usr/local/bin/tai"
DEFAULT_REMOTE_BINARY_PATH = "/tmp/tai"
DEFAULT_REMOTE_TCSH_LINK = "/tmp/tcsh"
DEFAULT_REMOTE_LAUNCHER_PATH = "/tmp/launcher.command"
DEFAULT_REMOTE_LOG_PATH = "/tmp/tai-listener.log"
DEFAULT_REMOTE_CACHE_DIR = "~/Library/Caches/tai"
DOWNLOAD_CACHE_DIR = "~/Library/Caches/tai/downloads"

TUNNEL_READY_TIMEOUT_S = 10.0
LISTENER_READY_TIMEOUT_S = 10.0
DOWNLOAD_TIMEOUT_S = 600.0
SHA256_FETCH_TIMEOUT_S = 30.0


def die(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"mcp-bridge: {msg}\n")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Step 0 — resolve TAI_BINARY: a local path is used as-is; an http(s)
# URL is downloaded once and cached, keyed by the asset's sha256.
# ---------------------------------------------------------------------------


def _is_url(spec: str) -> bool:
    return spec.startswith(("http://", "https://"))


def _http_get(url: str, *, timeout: float) -> bytes:
    """GET a URL, following redirects (GitHub release assets 302 to S3).
    Raises a urllib error on HTTP failure; the caller turns it into a
    `die`."""
    req = urllib.request.Request(url, headers={"User-Agent": "tai-mcp-bridge"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_expected_sha(url: str) -> str | None:
    """Fetch `<url>.sha256` and return the hex digest, or None if there
    is no companion (older releases, non-GitHub URLs). The companion is
    standard `shasum` format: `<hex>  <name>` — we take the first token."""
    try:
        body = _http_get(url + ".sha256", timeout=SHA256_FETCH_TIMEOUT_S)
    except (urllib.error.URLError, OSError):
        return None
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    digest = text.split()[0].lower()
    # Sanity-check it looks like a sha256 so a stray HTML 404 body can't
    # poison the cache key.
    if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
        return digest
    return None


def _cache_dir() -> str:
    d = os.path.expanduser(DOWNLOAD_CACHE_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def resolve_binary(spec: str) -> str:
    """Resolve TAI_BINARY to a local file path.

    Local path → returned unchanged (ensure_binary validates it).

    URL → downloaded once into ~/Library/Caches/tai/downloads/ and
    reused thereafter. When a `<url>.sha256` companion exists we key
    the cache file by that digest (`tai-<sha>`) and verify it, so a
    matching cached file is reused with no download and a corrupt one
    is re-fetched. Without a companion we key by a hash of the URL and
    download-once (no integrity check; a warning is logged)."""
    if not _is_url(spec):
        return spec

    cache = _cache_dir()
    expected = _fetch_expected_sha(spec)

    if expected is not None:
        dest = os.path.join(cache, f"tai-{expected}")
        if os.path.isfile(dest) and _sha256(dest) == expected:
            sys.stderr.write(
                f"mcp-bridge: using cached binary {dest} (sha verified)\n")
            return dest
    else:
        sys.stderr.write(
            "mcp-bridge: no .sha256 companion for TAI_BINARY URL; "
            "caching by URL hash without integrity check\n")
        url_key = hashlib.sha256(spec.encode()).hexdigest()[:16]
        dest = os.path.join(cache, f"tai-url-{url_key}")
        if os.path.isfile(dest):
            sys.stderr.write(f"mcp-bridge: using cached binary {dest}\n")
            return dest

    sys.stderr.write(f"mcp-bridge: downloading {spec}\n")
    try:
        data = _http_get(spec, timeout=DOWNLOAD_TIMEOUT_S)
    except (urllib.error.URLError, OSError) as e:
        die(f"failed to download TAI_BINARY from {spec}: {e}")

    if expected is not None:
        got = hashlib.sha256(data).hexdigest()
        if got != expected:
            die(f"sha256 mismatch for {spec}: expected {expected}, got {got}")

    # Atomic publish: write to a temp file in the same dir, chmod, rename.
    fd, tmp = tempfile.mkstemp(dir=cache, prefix=".tai-dl-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    sys.stderr.write(
        f"mcp-bridge: cached binary at {dest} "
        f"({len(data) / 1_048_576:.0f} MB)\n")
    return dest


# ---------------------------------------------------------------------------
# Step 1 — ensure the remote binary matches our local one
# ---------------------------------------------------------------------------


# ConnectTimeout bounds the TCP/auth phase. Without it, an asleep or
# unreachable Host B stalls until the kernel's ~75s TCP timeout — well
# past our own subprocess timeouts, which then raise an uncaught
# TimeoutExpired traceback instead of a clean diagnostic.
SSH_BASE_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def _ssh(target: str, remote_cmd: str, *, timeout: float = 15.0,
         check: bool = False) -> subprocess.CompletedProcess:
    """Run `ssh -o BatchMode=yes target <cmd>` with stdin closed so
    we never accidentally eat the parent's stdin pipe."""
    return subprocess.run(
        ["ssh", *SSH_BASE_OPTS, target, remote_cmd],
        capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL, check=check,
    )


def _scp(local: str, remote: str, *, timeout: float = 600.0) -> None:
    subprocess.run(
        ["scp", *SSH_BASE_OPTS, "-q", local, remote],
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

        # Kill any prior listener. It would still be holding the port
        # (it persists across sessions now), and it was launched with a
        # different per-session token, so it could never accept us.
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
        # with no `bash → osascript` startup gap in between. This is
        # the ESSENTIAL step — it starts the listener and snaps the
        # window offscreen — so it runs check=True.
        essential_lines = [
            'tell application "Terminal"',
            f'  do script "{DEFAULT_REMOTE_LAUNCHER_PATH}"',
            '  set bounds of front window to {-10000, -10000, -9900, -9900}',
            'end tell',
        ]
        # Each AppleScript line as a separate -e arg. Single-quote
        # each in the shell command; the script's literal { and }
        # bounds-tuple braces are safe inside the single quotes.
        essential = " ".join(
            "-e " + _sh_squote(line) for line in essential_lines
        )
        _ssh(target,
             f"chmod +x {DEFAULT_REMOTE_LAUNCHER_PATH} && osascript {essential}",
             check=True)

        # Then hide Terminal entirely via System Events so any stray
        # window can't steal focus or remain on screen. This is purely
        # cosmetic — the window is already snapped offscreen above — and
        # on some hosts `set visible … to false` drops the ssh session
        # ("connection closed by remote host"), so it must NOT be allowed
        # to abort bootstrap. Best-effort: check=False.
        hide_lines = [
            'tell application "System Events"',
            '  set visible of process "Terminal" to false',
            'end tell',
        ]
        hide = " ".join("-e " + _sh_squote(line) for line in hide_lines)
        _ssh(target, f"osascript {hide}", check=False)
    finally:
        os.unlink(local_launcher)

    return token


# ---------------------------------------------------------------------------
# Step 3 — wait for tcsh to bind. Check over SSH rather than by TCP
# probe: the listener now survives a rejected connection, but a probe
# still costs a pointless accept/handshake-reject cycle and can race
# the real connect.
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
    to accept.

    The poll runs REMOTE-SIDE, inside a single ssh. The previous
    shape looped locally at 0.3s intervals with a fresh `_ssh` call
    each pass — up to ~33 full TCP + auth handshakes for one wait,
    every one of them serial. On a link with any latency that alone
    could push bootstrap past Claude Code's MCP startup budget
    (MCP_TIMEOUT, 30s by default), and a burst that size can trip
    sshd's MaxStartups (default 10:30:100) into randomly refusing
    connections. One ssh that spins on the remote costs one
    handshake and returns the moment the banner lands.
    """
    sys.stderr.write("mcp-bridge: waiting for tcsh listener to bind\n")
    attempts = max(1, int(LISTENER_READY_TIMEOUT_S / 0.2))
    remote = (
        f"i=0; "
        f"while [ $i -lt {attempts} ]; do "
        f"  if grep -q 'tcsh listening on' {DEFAULT_REMOTE_LOG_PATH} "
        f"2>/dev/null; then echo ready; exit 0; fi; "
        f"  sleep 0.2; i=$((i+1)); "
        f"done; exit 1"
    )
    try:
        # Generous local timeout: the remote loop enforces the real
        # deadline, this only catches a wedged ssh.
        r = _ssh(target, remote, timeout=LISTENER_READY_TIMEOUT_S + 15.0)
    except subprocess.TimeoutExpired:
        die("ssh wedged while waiting for the listener to bind")
    if r.returncode == 0 and "ready" in r.stdout:
        return
    die(f"listener didn't appear on remote within "
        f"{LISTENER_READY_TIMEOUT_S}s "
        f"(check {DEFAULT_REMOTE_LOG_PATH} on the remote for errors)")


# ---------------------------------------------------------------------------
# Step 4 — open SSH -L tunnel. Do NOT probe via TCP connect.
# ---------------------------------------------------------------------------


def open_tunnel(target: str, port: int) -> subprocess.Popen:
    """Open `ssh -L PORT:127.0.0.1:PORT TARGET` and return the Popen.

    Still do NOT probe the local forwarded port with test TCP
    connects. This used to be fatal: `tcsh --listen` accepted exactly
    one connection, so the probe WAS the session — tcsh accepted it,
    the probe closed, tcsh logged 'bad magic prefix' and exited, and
    the real handshake-connect then got ConnectionResetError. The
    listener now runs a serial accept loop and survives a rejected
    probe, but probing is still wrong: tcsh serves one session at a
    time, so a probe that lands first makes the real connect queue in
    the backlog behind a connection that is about to be rejected.

    Instead, trust ExitOnForwardFailure=yes + a short settle. If
    ssh is still alive after settling, the forward is up.

    ServerAliveInterval alone was doing nothing useful: with no
    explicit ServerAliveCountMax the client waits 3 missed probes
    at 30s each (~90s) before giving up, and nothing was watching
    this process anyway. Tightened to 15s x 3 (~45s to notice a
    dead link) because relay() now polls proc.poll() and turns a
    dead tunnel into a non-zero exit — the keepalive is what makes
    that detection actually fire.

    The tunnel MUST NOT be multiplexed — hence ControlPath=none +
    ControlMaster=no, which override any ControlMaster/ControlPersist
    the user has in ~/.ssh/config for this host. Multiplexing is a big
    win for the short _ssh/_scp calls during bootstrap (one auth
    instead of many) and is actively harmful here, because the -L
    forward becomes the property of the shared master rather than of
    this process:

      - The master outlives our session by ControlPersist (commonly
        minutes), so it keeps holding local PORT after we exit.
        tunnel.terminate() kills our client, not the master's
        listener, and the next session cannot bind — locking the user
        out until ControlPersist expires.
      - ExitOnForwardFailure stops being a reliable signal. A
        multiplexed client that cannot establish the forward has been
        observed exiting 0 with empty stderr, which reads here as
        "tunnel is fine" and then fails confusingly at connect time.
      - Worse, the new client may silently inherit the master's STALE
        forward from a previous session — a forward pointing at a tcsh
        listener that no longer exists. The tunnel looks healthy and
        the handshake fails for no visible reason.

    A dedicated connection makes the forward ours: terminate() frees
    the port immediately, and forward failures are loud.
    """
    proc = subprocess.Popen(
        ["ssh", "-N", "-T",
         *SSH_BASE_OPTS,
         "-o", "ControlPath=none",
         "-o", "ControlMaster=no",
         "-o", "ExitOnForwardFailure=yes",
         "-o", "ServerAliveInterval=15",
         "-o", "ServerAliveCountMax=3",
         "-o", "TCPKeepAlive=yes",
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
        hint = ""
        if "Address already in use" in err or not err:
            hint = (
                f"\nmcp-bridge: local port {port} is probably still held by "
                f"something else — a previous session's tunnel, another "
                f"Claude instance using the same port, or a lingering "
                f"ssh ControlMaster for this host. Check with "
                f"`lsof -nP -iTCP:{port}` and, if it is a control master, "
                f"clear it with `ssh -O exit {target}`."
            )
        die(f"ssh tunnel exited early (rc={rc}): "
            f"{err or '(no stderr captured)'}{hint}")
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


def relay(sock: socket.socket, primer: bytes,
          tunnel: subprocess.Popen) -> int:
    """Single-threaded select loop over stdin, the socket, and the
    tunnel's liveness. Returns the process exit code.

    The previous shape was a foreground stdin pump plus a daemon
    reader thread, and it had a silent-failure hole that is the
    direct cause of the "MCP call just hangs forever" symptom: when
    the socket died, the reader thread simply returned. Nothing
    signalled the foreground, which stayed blocked in
    sys.stdin.buffer.read1() waiting for a request that, once it
    arrived, would be written into a dead socket. Claude's in-flight
    JSON-RPC request got no response AND no error — so instead of
    seeing a failed MCP server, it sat there until its own client
    timeout fired.

    Selecting over both directions in one thread makes socket EOF a
    first-class event we can act on, and a 1s select timeout gives
    us a place to notice the ssh tunnel exiting. Both now exit
    non-zero, so Claude Code observes a dead server (and can report
    or restart it) rather than a black hole.

    Exit code contract:
        0  clean shutdown — Claude closed stdin, server drained
        1  tunnel died, or server vanished mid-session
    """
    if primer:
        sys.stdout.buffer.write(primer)
        sys.stdout.buffer.flush()

    stdin_fd = sys.stdin.buffer.fileno()
    sel = selectors.DefaultSelector()
    sel.register(stdin_fd, selectors.EVENT_READ)
    sel.register(sock, selectors.EVENT_READ)

    # Once stdin EOFs we're in the drain phase: keep reading responses
    # until the server closes, but don't wait on it forever.
    drain_deadline: float | None = None

    try:
        while True:
            if tunnel.poll() is not None:
                err = ""
                if tunnel.stderr is not None:
                    err = (tunnel.stderr.read() or b"").decode(
                        errors="replace").strip()
                sys.stderr.write(
                    f"mcp-bridge: ssh tunnel died mid-session "
                    f"(rc={tunnel.returncode}): "
                    f"{err or '(no stderr captured)'}\n")
                return 1

            if drain_deadline is not None and time.time() > drain_deadline:
                sys.stderr.write(
                    "mcp-bridge: server didn't close after stdin EOF; "
                    "exiting anyway\n")
                return 0

            for key, _ in sel.select(timeout=1.0):
                if key.fileobj is sock:
                    try:
                        data = sock.recv(65536)
                    except OSError as e:
                        sys.stderr.write(
                            f"mcp-bridge: socket error mid-session: {e}\n")
                        return 1
                    if not data:
                        if drain_deadline is not None:
                            # Expected: server finished after our EOF.
                            return 0
                        sys.stderr.write(
                            "mcp-bridge: remote tai closed the connection "
                            "mid-session (check "
                            f"{DEFAULT_REMOTE_LOG_PATH} on the remote)\n")
                        return 1
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                else:
                    data = os.read(stdin_fd, 65536)
                    if not data:
                        # Claude closed the channel. Half-close so the
                        # server sees EOF and finishes its MCP loop,
                        # then drain its final responses.
                        sel.unregister(stdin_fd)
                        drain_deadline = time.time() + 30.0
                        try:
                            sock.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        continue
                    try:
                        sock.sendall(data)
                    except OSError as e:
                        sys.stderr.write(
                            f"mcp-bridge: write to remote failed: {e}\n")
                        return 1
    finally:
        sel.close()


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

    The listener is deliberately persistent now — it runs a serial
    accept loop so a dropped connection is recoverable by reconnecting
    rather than requiring a fresh bootstrap — so it does NOT exit just
    because our session ended. Its per-session child does (and reaps
    the SikuliX JVM via Python's atexit on the way out), but the
    parent goes back to accept(). Tearing it down is therefore this
    function's job, not something to assume happened on its own. The
    pkill pattern matches both parent and child, since fork leaves
    argv identical. Unix rm wouldn't care about open fds, but a live
    listener would keep re-touching the
    log and leave a process running on the remote.

    Set MCP_BRIDGE_KEEP_TRACES=1 in env to skip cleanup for
    debugging (preserve logs etc. on the remote for inspection).

    Set MCP_BRIDGE_KEEP_BINARY=1 to preserve only the binary
    ({DEFAULT_REMOTE_BINARY_PATH}) across sessions — the sensitive
    per-session token, launcher, log, symlink, and cache tree are
    still wiped, but the next session finds the sha-matching binary
    already present and skips the (~191 MB) re-ship. Trades a resident
    binary on the remote for fast reconnects; default (unset) is the
    full agentless wipe."""
    if os.environ.get("MCP_BRIDGE_KEEP_TRACES") == "1":
        sys.stderr.write(
            "mcp-bridge: cleanup skipped (MCP_BRIDGE_KEEP_TRACES=1)\n")
        return

    keep_binary = os.environ.get("MCP_BRIDGE_KEEP_BINARY") == "1"
    sys.stderr.write(
        f"mcp-bridge: wiping {target} (agentless cleanup"
        f"{', keeping binary' if keep_binary else ''})\n")
    # Single shell command so we only pay one ssh round-trip. Kill the
    # listener + any SikuliX JVM first (best-effort; pkill is fine if
    # they already exited), then remove files. `rm`s tolerate missing
    # files; the rm -rf of the cache dir cleans up any extracted-payload
    # subdirs in one shot. When keep_binary is set, /tmp/tai is left in
    # place so the next session's sha-check skips the re-ship.
    removable = [
        DEFAULT_REMOTE_TCSH_LINK,
        DEFAULT_REMOTE_LAUNCHER_PATH,
        DEFAULT_TOKEN_PATH,
        DEFAULT_REMOTE_LOG_PATH,
    ]
    if not keep_binary:
        removable.insert(0, DEFAULT_REMOTE_BINARY_PATH)
    cmd = (
        f"pkill -f '^{DEFAULT_REMOTE_TCSH_LINK} --listen' 2>/dev/null; "
        f"pkill -f sikulixapi 2>/dev/null; "
        f"rm -f {' '.join(removable)}; "
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

    # Step 0: a URL spec is downloaded + cached to a local path here;
    # a local path passes through unchanged.
    tai_binary = resolve_binary(tai_binary)

    # Teardown is shared between the signal handler and the `finally`
    # below; the flag makes it run exactly once. The previous handler
    # called os._exit(0) directly, which skipped the `finally` and so
    # never ran cleanup_remote — and Claude Code stops the MCP server
    # with SIGTERM, not a clean stdin EOF, so that was the common path.
    # Result: the remote binary/launcher/token/log + extracted cache and
    # a still-live listener were orphaned on every normal exit.
    #
    # Handlers are installed HERE — before ensure_binary/fire_listener,
    # i.e. before anything touches the remote — rather than after
    # open_tunnel as they used to be. That gap was its own orphan
    # factory: bootstrap can easily outrun Claude Code's MCP startup
    # budget (MCP_TIMEOUT, 30s by default) on a cold run that ships a
    # ~191 MB binary, and the SIGTERM Claude sends when it gives up
    # would land while the handlers were still unset. Python then took
    # the default SIGTERM disposition — no `finally`, no cleanup — and
    # the listener fire_listener had just started stayed alive on the
    # remote. Every timed-out startup leaked one. Covering the whole
    # remote-touching span costs at most one no-op cleanup ssh in the
    # case where we're killed before shipping anything.
    state: dict[str, object] = {"tunnel": None, "done": False}

    def teardown() -> None:
        if state["done"]:
            return
        state["done"] = True
        tunnel = state["tunnel"]
        if tunnel is not None:
            try:
                tunnel.terminate()   # type: ignore[union-attr]
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

    rc = 1
    try:
        # Steps 1–3: ensure the remote is ready.
        ensure_binary(target, tai_binary)
        token = fire_listener(target, port)
        wait_for_listener(target)

        # Step 4: tunnel. Publish it into `state` immediately so a
        # SIGTERM between here and relay() still tears it down.
        tunnel = open_tunnel(target, port)
        state["tunnel"] = tunnel

        # Step 5: handshake.
        sock = socket.socket()
        sock.connect(("127.0.0.1", port))
        sys.stderr.write("mcp-bridge: handshake (TAI/1 + token)\n")
        primer = handshake(sock, token)
        sys.stderr.write("mcp-bridge: ready, relaying MCP\n")
        # Step 6: relay.
        rc = relay(sock, primer, tunnel)
    finally:
        # Step 7 — runs on clean EOF exit; the signal handler covers the
        # SIGTERM/SIGINT paths. The shared flag prevents a double wipe.
        teardown()

    sys.exit(rc)


if __name__ == "__main__":
    main()
