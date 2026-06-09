"""Tests for `holo.mcp_connect` — stdio↔TCP bridge."""

from __future__ import annotations

import io
import socket
import threading
import time

import pytest

from holo import mcp_connect
from holo.mcp_wire import WIRE_MAGIC

# --- parse_endpoint ---------------------------------------------------


def test_parse_endpoint_host_port():
    assert mcp_connect.parse_endpoint("localhost:7777") == ("localhost", 7777)


def test_parse_endpoint_ipv4():
    assert mcp_connect.parse_endpoint("127.0.0.1:1234") == ("127.0.0.1", 1234)


def test_parse_endpoint_ipv6_bracketed():
    assert mcp_connect.parse_endpoint("[::1]:9999") == ("::1", 9999)


@pytest.mark.parametrize(
    "bad",
    [
        "noport",
        "localhost:",
        "localhost:abc",
        "localhost:0",
        "localhost:99999",
        "[::1]7777",
        "[::1",
    ],
)
def test_parse_endpoint_invalid(bad):
    with pytest.raises(ValueError):
        mcp_connect.parse_endpoint(bad)


# --- end-to-end bridge ------------------------------------------------


class _FakeServer:
    """Accepts one TCP connection, captures inbound bytes, optionally
    replies once."""

    def __init__(self, reply: bytes = b"") -> None:
        self.reply = reply
        self.received: bytes = b""
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._done = threading.Event()
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        try:
            conn.settimeout(2.0)
            buf = bytearray()
            while True:
                try:
                    chunk = conn.recv(4096)
                except (TimeoutError, OSError):
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                # Once we've seen the magic prefix + at least one extra
                # line, send our reply and stop reading.
                if self.reply and buf.endswith(b"\n") and len(buf) > len(WIRE_MAGIC):
                    conn.sendall(self.reply)
                    break
            self.received = bytes(buf)
        finally:
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            conn.close()
            self._done.set()

    def wait(self, timeout: float = 3.0) -> None:
        self._done.wait(timeout=timeout)

    def close(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass


def _patch_stdio(monkeypatch, stdin_bytes: bytes):
    """Redirect sys.stdin / sys.stdout to in-memory byte buffers so we
    can drive `mcp_connect.run` without touching the real terminal."""
    import sys

    stdin_buf = io.BytesIO(stdin_bytes)
    stdout_buf = io.BytesIO()

    class _StdinShim:
        buffer = stdin_buf

    class _StdoutShim:
        buffer = stdout_buf

    monkeypatch.setattr(sys, "stdin", _StdinShim())
    monkeypatch.setattr(sys, "stdout", _StdoutShim())
    return stdin_buf, stdout_buf


def test_run_sends_magic_prefix_first(monkeypatch):
    """The first bytes on the wire must be the magic prefix."""
    server = _FakeServer(reply=b"")
    try:
        msg = b'{"jsonrpc":"2.0","id":1,"method":"x"}\n'
        _patch_stdio(monkeypatch, stdin_bytes=msg)

        rc = mcp_connect.run(f"127.0.0.1:{server.port}")

        assert rc == 0
        server.wait()
        assert server.received.startswith(WIRE_MAGIC)
        assert server.received[len(WIRE_MAGIC):] == msg
    finally:
        server.close()


def test_run_pipes_server_reply_to_stdout(monkeypatch):
    """Bytes from the socket must reach our stdout."""
    reply = b'{"jsonrpc":"2.0","id":1,"result":"ok"}\n'
    server = _FakeServer(reply=reply)
    try:
        msg = b'{"jsonrpc":"2.0","id":1,"method":"x"}\n'
        _, stdout_buf = _patch_stdio(monkeypatch, stdin_bytes=msg)

        rc = mcp_connect.run(f"127.0.0.1:{server.port}")

        assert rc == 0
        server.wait()
        # Give the inbound copier a beat to flush.
        time.sleep(0.05)
        assert stdout_buf.getvalue() == reply
    finally:
        server.close()


def test_run_returns_2_on_invalid_endpoint(monkeypatch, capsys):
    _patch_stdio(monkeypatch, stdin_bytes=b"")
    rc = mcp_connect.run("not-a-host-port")
    assert rc == 2
    assert "invalid endpoint" in capsys.readouterr().err


def test_run_returns_1_on_connection_refused(monkeypatch, capsys):
    """Pick a definitely-not-listening port, expect a clean error."""
    _patch_stdio(monkeypatch, stdin_bytes=b"")
    # Bind a socket then close it to get a port we know is free, then
    # use that port number — connect should fail.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    rc = mcp_connect.run(f"127.0.0.1:{port}")

    assert rc == 1
    assert f"127.0.0.1:{port}" in capsys.readouterr().err


def test_run_round_trips_against_real_listener(monkeypatch):
    """End-to-end: spin up `holo mcp --listen` in a thread and have
    `holo connect` talk to it. Verifies the two halves agree on the
    wire format. We use MCP `initialize` since it doesn't touch the
    daemon."""
    import json

    from holo import mcp_server

    # Find a free port and start the listener.
    free = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()

    stop = threading.Event()
    t = threading.Thread(
        target=mcp_server.run_tcp,
        kwargs={"port": port, "stop_event": stop},
        daemon=True,
    )
    t.start()

    # Wait for listener to be ready.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            probe = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            probe.close()
            break
        except OSError:
            time.sleep(0.05)

    init_msg = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "holo-test", "version": "0"},
                },
            }
        )
        + "\n"
    ).encode()

    _, stdout_buf = _patch_stdio(monkeypatch, stdin_bytes=init_msg)
    try:
        rc = mcp_connect.run(f"127.0.0.1:{port}")
        assert rc == 0
        # Drain the inbound thread's last flush.
        time.sleep(0.05)
        out = stdout_buf.getvalue()
        assert out, "no response from server"
        first_line = out.split(b"\n", 1)[0]
        msg = json.loads(first_line.decode())
        assert msg["id"] == 1
        assert msg["jsonrpc"] == "2.0"
        assert "result" in msg
    finally:
        stop.set()
        t.join(timeout=3.0)
