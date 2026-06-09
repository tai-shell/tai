"""End-to-end tests for `holo mcp --listen PORT`.

We run `mcp_server.run_tcp` in a thread, talk to it over a real TCP
socket, and assert: bad handshakes are dropped, good ones speak MCP,
and the listener survives a misbehaving client (so the next valid
client still gets through).

Tool-level behaviour is covered by `test_mcp_server.py`. Here we
just exercise the transport.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from holo import mcp_server
from holo.mcp_wire import WIRE_MAGIC


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def listener():
    """Spin up `run_tcp` on a random port. Stop it on teardown.

    Note: `run_tcp` constructs a real `Daemon` lazily — first tool
    call would start the WS server. We avoid that by only sending
    `initialize` (a protocol-level handshake that doesn't touch the
    daemon).
    """
    port = _free_port()
    stop = threading.Event()
    t = threading.Thread(
        target=mcp_server.run_tcp,
        kwargs={"port": port, "stop_event": stop},
        daemon=True,
        name="test-listener",
    )
    t.start()

    # Wait for the listener to be ready by trying to connect briefly.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            probe = socket.create_connection(("127.0.0.1", port), timeout=0.2)
        except OSError:
            time.sleep(0.05)
            continue
        probe.close()
        break
    else:
        stop.set()
        raise RuntimeError("listener never came up")

    yield port

    stop.set()
    t.join(timeout=3.0)


def _send_mcp_initialize(sock: socket.socket, msg_id: int = 1) -> None:
    """Send a minimal MCP initialize request that FastMCP will accept."""
    payload = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "holo-test", "version": "0"},
        },
    }
    sock.sendall((json.dumps(payload) + "\n").encode())


def _read_one_line(sock: socket.socket, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def test_bad_handshake_is_dropped(listener):
    port = listener
    sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        sock.sendall(b"POST / HTTP/1.1\r\n\r\n")
        sock.settimeout(2.0)
        # Server should close without sending anything back.
        data = sock.recv(64)
        assert data == b""
    finally:
        sock.close()


def test_listener_survives_bad_handshake(listener):
    """A misbehaving client must not take down the listener — the
    next valid client must still get an MCP handshake."""
    port = listener

    bad = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        bad.sendall(b"GET / HTTP/1.1\r\n\r\n")
        bad.settimeout(2.0)
        bad.recv(64)  # drain close
    finally:
        bad.close()

    good = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        good.sendall(WIRE_MAGIC)
        _send_mcp_initialize(good)
        line = _read_one_line(good)
        assert line, "expected an initialize response"
        msg = json.loads(line.decode())
        assert msg.get("jsonrpc") == "2.0"
        assert msg.get("id") == 1
        assert "result" in msg
    finally:
        good.close()


def test_initialize_round_trip(listener):
    """Send a real MCP initialize over the TCP transport, expect a
    valid response. This proves the wire format works end-to-end:
    handshake → MCP server → reply → client."""
    port = listener
    sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        sock.sendall(WIRE_MAGIC)
        _send_mcp_initialize(sock, msg_id=42)
        line = _read_one_line(sock)
        assert line, "expected initialize response"
        msg = json.loads(line.decode())
        assert msg["jsonrpc"] == "2.0"
        assert msg["id"] == 42
        result = msg["result"]
        # FastMCP populates serverInfo and protocolVersion.
        assert result.get("serverInfo", {}).get("name") == "holo"
        assert "protocolVersion" in result
    finally:
        sock.close()


def test_reconnect_works_after_disconnect(listener):
    """Daemon state survives across connection lifetimes — we don't
    test daemon state directly here (no tool calls), but we do
    verify the listener accepts a fresh connection after the previous
    one closes."""
    port = listener

    s1 = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        s1.sendall(WIRE_MAGIC)
        _send_mcp_initialize(s1, msg_id=1)
        _read_one_line(s1)
    finally:
        s1.close()

    # Give the listener a moment to see the close and loop back to accept.
    time.sleep(0.1)

    s2 = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        s2.sendall(WIRE_MAGIC)
        _send_mcp_initialize(s2, msg_id=2)
        line = _read_one_line(s2)
        msg = json.loads(line.decode())
        assert msg["id"] == 2
    finally:
        s2.close()
