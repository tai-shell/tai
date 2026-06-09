"""Tests for `holo.mcp_remote` — the stdio MCP transport bridge.

We synthesise a fake "remote MCP server" by spawning a Python helper
that emits a banner then echoes one JSON-RPC line back. The bridge is
expected to:

* hide the banner from our stdout
* pass the JSON envelope through unchanged
* exit cleanly when the child exits

Failure paths (no JSON, child exits early, command not found) get
their own cases.
"""

from __future__ import annotations

import io
import sys
import time
from contextlib import contextmanager

from holo import mcp_remote

# --- helpers ---------------------------------------------------------

# A child command that emits N banner lines then echoes one JSON line
# read from stdin and exits. Implemented as a Python -c invocation so
# the test stays self-contained — no shell quoting drama, works on
# CI runners that lack bash niceties.
def _echo_child_argv(banner_lines: list[str]) -> list[str]:
    parts = []
    for line in banner_lines:
        # write banner verbatim to stdout
        parts.append(f"sys.stdout.write({line!r}); sys.stdout.flush()")
    # read one line and echo back as JSON-RPC result
    parts.append(
        "line = sys.stdin.readline().strip();"
        " sys.stdout.write("
        "'{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"echo\":\"' + line + '\"}}\\n'"
        "); sys.stdout.flush()"
    )
    code = "import sys; " + "; ".join(parts)
    return [sys.executable, "-c", code]


def _failing_child_argv(rc: int = 7) -> list[str]:
    code = (
        "import sys; "
        "sys.stderr.write('boom\\n'); "
        f"sys.exit({rc})"
    )
    return [sys.executable, "-c", code]


def _silent_child_argv() -> list[str]:
    """Child that sleeps without writing or exiting — triggers timeout."""
    code = "import time; time.sleep(10)"
    return [sys.executable, "-c", code]


@contextmanager
def _redirected_stdio(in_bytes: bytes):
    """Replace stdin/stdout/stderr with byte buffers we can inspect."""
    real_in, real_out, real_err = sys.stdin, sys.stdout, sys.stderr

    in_io = io.BytesIO(in_bytes)
    out_io = io.BytesIO()
    err_io = io.BytesIO()

    class _BufWrap:
        def __init__(self, b):
            self.buffer = b

        def write(self, text):
            if isinstance(text, str):
                self.buffer.write(text.encode("utf-8"))
            else:
                self.buffer.write(text)

        def flush(self):
            pass

    sys.stdin = _BufWrap(in_io)  # type: ignore[assignment]
    sys.stdout = _BufWrap(out_io)  # type: ignore[assignment]
    sys.stderr = _BufWrap(err_io)  # type: ignore[assignment]
    try:
        yield out_io, err_io
    finally:
        sys.stdin, sys.stdout, sys.stderr = real_in, real_out, real_err


# --- happy path ------------------------------------------------------

def test_strips_banner_and_passes_json_through():
    """Banner lines on stdout are filtered; JSON envelopes pass."""
    argv = _echo_child_argv([
        "Welcome to MOTD\n",
        "Last login: never\n",
    ])
    request_line = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    with _redirected_stdio(request_line) as (out, err):
        rc = mcp_remote.run(argv, startup_timeout_s=10.0)
    assert rc == 0, err.getvalue().decode()
    out_text = out.getvalue().decode()
    # The banner must NOT appear on our stdout — that's the whole job.
    assert "Welcome" not in out_text
    assert "Last login" not in out_text
    # The JSON envelope must arrive unchanged.
    assert '"echo"' in out_text
    assert out_text.startswith("{")


def test_no_banner_at_all_still_works():
    """A clean transport with no banner: first line is JSON immediately."""
    argv = _echo_child_argv([])
    request_line = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    with _redirected_stdio(request_line) as (out, err):
        rc = mcp_remote.run(argv, startup_timeout_s=10.0)
    assert rc == 0, err.getvalue().decode()
    assert '"echo"' in out.getvalue().decode()


# --- failure paths ---------------------------------------------------

def test_command_not_found_returns_127():
    """A truly missing command surfaces a clean error, no hang."""
    with _redirected_stdio(b"") as (_out, err):
        rc = mcp_remote.run(["definitely-not-a-real-command-12345"])
    assert rc == 127
    assert b"command not found" in err.getvalue()


def test_no_args_returns_2():
    """Calling with empty argv prints usage."""
    with _redirected_stdio(b"") as (_out, err):
        rc = mcp_remote.run([])
    assert rc == 2
    assert b"no command provided" in err.getvalue()


def test_child_exits_before_handshake():
    """Child dies without sending JSON: we report the exit, not a timeout."""
    with _redirected_stdio(b"") as (_out, err):
        rc = mcp_remote.run(_failing_child_argv(rc=7), startup_timeout_s=5.0)
    # We surface the child's exit code rather than masking with 124.
    assert rc == 7
    err_text = err.getvalue().decode()
    assert "exited" in err_text
    # Child's stderr should reach our stderr.
    assert "boom" in err_text


def test_handshake_timeout_kills_child_and_reports():
    """A child that never speaks gets killed after the timeout."""
    with _redirected_stdio(b"") as (_out, err):
        t0 = time.monotonic()
        rc = mcp_remote.run(_silent_child_argv(), startup_timeout_s=0.5)
        elapsed = time.monotonic() - t0
    assert rc == 124
    # Should have killed within ~the timeout, not waited for child exit.
    assert elapsed < 3.0
    err_text = err.getvalue().decode()
    assert "no MCP envelope" in err_text


def test_diagnostic_includes_captured_banner():
    """Banner content captured before timeout shows up in the diagnostic."""
    code = (
        "import sys, time; "
        "sys.stdout.write('UNIQUE-BANNER-CONTENT\\n'); "
        "sys.stdout.flush(); "
        "time.sleep(10)"
    )
    argv = [sys.executable, "-c", code]
    with _redirected_stdio(b"") as (_out, err):
        rc = mcp_remote.run(argv, startup_timeout_s=0.5)
    assert rc == 124
    assert b"UNIQUE-BANNER-CONTENT" in err.getvalue()
