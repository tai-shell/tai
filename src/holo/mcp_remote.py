"""Stdio MCP bridge: proxies a child process's stdio onto our own.

Used by `holo mcp-remote -- <command>` to ferry MCP traffic between a
local agent (Claude Code, Codex, …) and a remote `holo mcp` reachable
via some user-supplied transport (`ssh`, `kubectl exec`, `aws ssm`, a
custom proxy script — we don't care which). The bridge is transport-
agnostic: anything that produces line-delimited JSON-RPC on its stdout
will work.

Three things this does that a plain `exec` doesn't:

1. **Banner stripping.** SSH MOTDs, kubectl warnings, etc. write to
   stdout before the remote MCP server speaks. Without filtering, the
   first line the agent receives isn't valid MCP and the session
   collapses. We read line-by-line, skip anything that doesn't start
   with `{`, and switch to passthrough on the first JSON envelope.

2. **Startup timeout with diagnostic dump.** If the transport halts on
   an interactive prompt (password / passphrase / 2FA), or the remote
   `holo` is missing, or some early failure happens, the agent today
   sees a silent hang. We bound first-envelope arrival to
   `startup_timeout_s` (default 15s) and on timeout kill the child and
   write the captured banner + a diagnostic to *our* stderr.

3. **No tty.** Pipes only — never a pty. Interactive auth can't prompt;
   it'll fail loud instead of hanging silently. Users who need
   non-interactive auth set up keys / agents / ControlMasters ahead of
   time.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import threading
from collections import deque

DEFAULT_STARTUP_TIMEOUT_S = 15.0
MAX_BANNER_BYTES = 64 * 1024
TAIL_BYTES = 4096


def run(argv: list[str], startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S) -> int:
    """Spawn `argv`, bridge its stdio to ours. Returns child exit code."""
    if not argv:
        sys.stderr.write(
            "holo mcp-remote: no command provided after `--`\n"
            "example: holo mcp-remote -- ssh -A hostA holo mcp\n"
        )
        return 2

    try:
        proc = subprocess.Popen(  # noqa: S603 — caller controls argv
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as e:
        sys.stderr.write(
            f"holo mcp-remote: command not found: {argv[0]!r} ({e})\n"
        )
        return 127

    banner_chunks: deque[bytes] = deque()
    banner_total = [0]
    handshake_done = threading.Event()
    early_exit = threading.Event()

    out_buf = sys.stdout.buffer
    err_buf = sys.stderr.buffer
    in_buf = sys.stdin.buffer

    def pump_stdout() -> None:
        try:
            assert proc.stdout is not None
            while True:
                line = proc.stdout.readline()
                if not line:
                    return
                if handshake_done.is_set():
                    out_buf.write(line)
                    out_buf.flush()
                    continue
                if line.lstrip().startswith(b"{"):
                    handshake_done.set()
                    out_buf.write(line)
                    out_buf.flush()
                else:
                    if banner_total[0] < MAX_BANNER_BYTES:
                        banner_chunks.append(line)
                        banner_total[0] += len(line)
        except Exception:
            pass

    def pump_stderr() -> None:
        try:
            assert proc.stderr is not None
            while True:
                chunk = proc.stderr.read1(4096)
                if not chunk:
                    return
                err_buf.write(chunk)
                err_buf.flush()
        except Exception:
            pass

    def pump_stdin() -> None:
        try:
            assert proc.stdin is not None
            while True:
                chunk = in_buf.read1(4096)
                if not chunk:
                    return
                proc.stdin.write(chunk)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass

    def watch_exit() -> None:
        proc.wait()
        early_exit.set()

    threads = [
        threading.Thread(target=pump_stdout, daemon=True),
        threading.Thread(target=pump_stderr, daemon=True),
        threading.Thread(target=pump_stdin, daemon=True),
        threading.Thread(target=watch_exit, daemon=True),
    ]
    for t in threads:
        t.start()

    deadline = startup_timeout_s
    if handshake_done.wait(deadline):
        # Handshake clear — pump threads now in passthrough mode.
        # Wait for the child to exit (or for stdin to close which will
        # cause the child to see EOF and exit).
        proc.wait()
        return proc.returncode or 0

    if early_exit.is_set():
        # Child died before producing a JSON envelope.
        _emit_diagnostic(
            argv, startup_timeout_s, banner_chunks,
            cause=f"child exited with code {proc.returncode} before sending any MCP envelope",
        )
        return proc.returncode if proc.returncode is not None else 1

    # Timeout: kill and explain.
    try:
        proc.kill()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    _emit_diagnostic(argv, startup_timeout_s, banner_chunks)
    return 124


def _emit_diagnostic(
    argv: list[str],
    timeout_s: float,
    banner_chunks: deque[bytes],
    cause: str | None = None,
) -> None:
    if cause is None:
        cause = f"no MCP envelope from child within {timeout_s:.0f}s"
    cmd = " ".join(shlex.quote(a) for a in argv)
    tail = b"".join(banner_chunks)[-TAIL_BYTES:]
    sys.stderr.write(
        f"\nholo mcp-remote: {cause}\n"
        f"command: {cmd}\n"
        "likely cause:\n"
        "  - the command needed input (password / passphrase / 2FA) "
        "but no tty was attached\n"
        "  - the remote `holo` is not on PATH or `holo mcp` failed to start\n"
        "  - the command exited or wrote no JSON to stdout\n"
    )
    if tail:
        sys.stderr.write(
            f"captured stdout (last {min(len(tail), TAIL_BYTES)}B):\n"
        )
        sys.stderr.write(tail.decode("utf-8", errors="replace"))
        if not tail.endswith(b"\n"):
            sys.stderr.write("\n")
    else:
        sys.stderr.write("(no stdout captured before timeout)\n")
