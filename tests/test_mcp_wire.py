"""Tests for `holo.mcp_wire` — the magic-prefix handshake helpers."""

from __future__ import annotations

import socket
import threading
import time

from holo.mcp_wire import (
    HANDSHAKE_MAX_BYTES,
    HANDSHAKE_TIMEOUT_S,
    WIRE_MAGIC,
    is_valid_handshake,
    read_handshake,
)


def _socketpair() -> tuple[socket.socket, socket.socket]:
    a = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    a.bind(("127.0.0.1", 0))
    a.listen(1)
    port = a.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))
    server, _ = a.accept()
    a.close()
    return server, client


def test_valid_magic_round_trip():
    s, c = _socketpair()
    try:
        c.sendall(WIRE_MAGIC)
        line = read_handshake(s)
        assert line == WIRE_MAGIC
        assert is_valid_handshake(line)
    finally:
        s.close()
        c.close()


def test_subsequent_bytes_not_consumed():
    """read_handshake must stop at the first newline so MCP payload
    bytes that follow stay in the socket for downstream readers."""
    s, c = _socketpair()
    try:
        c.sendall(WIRE_MAGIC + b'{"jsonrpc":"2.0","id":1,"method":"x"}\n')
        line = read_handshake(s)
        assert line == WIRE_MAGIC
        # Whatever's left should still be readable from the socket.
        rest = b""
        s.settimeout(1.0)
        while not rest.endswith(b"\n"):
            chunk = s.recv(64)
            if not chunk:
                break
            rest += chunk
        assert rest == b'{"jsonrpc":"2.0","id":1,"method":"x"}\n'
    finally:
        s.close()
        c.close()


def test_http_post_rejected():
    """A browser fetch fires an HTTP request line first — that's what
    the magic prefix is designed to reject."""
    s, c = _socketpair()
    try:
        c.sendall(b"POST / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        line = read_handshake(s)
        assert not is_valid_handshake(line)
        assert line.startswith(b"POST ")
    finally:
        s.close()
        c.close()


def test_http_get_rejected():
    s, c = _socketpair()
    try:
        c.sendall(b"GET / HTTP/1.1\r\n")
        line = read_handshake(s)
        assert not is_valid_handshake(line)
    finally:
        s.close()
        c.close()


def test_ws_upgrade_rejected():
    """WebSocket upgrade is HTTP-shaped — same defense applies."""
    s, c = _socketpair()
    try:
        c.sendall(
            b"GET /chat HTTP/1.1\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n\r\n"
        )
        line = read_handshake(s)
        assert not is_valid_handshake(line)
    finally:
        s.close()
        c.close()


def test_slow_loris_bounded_by_timeout(monkeypatch):
    """A client that opens a connection but never sends \\n must not
    pin the listener forever. We monkey-patch the timeout to make
    the test fast."""
    import holo.mcp_wire as wire

    monkeypatch.setattr(wire, "HANDSHAKE_TIMEOUT_S", 0.2)
    s, c = _socketpair()
    try:
        # Send some bytes but no newline.
        c.sendall(b"HOLO/1")
        t0 = time.monotonic()
        line = read_handshake(s)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"read_handshake hung for {elapsed:.2f}s"
        assert not is_valid_handshake(line)
    finally:
        s.close()
        c.close()


def test_oversize_first_line_rejected():
    """A pathological client could try to spam bytes-without-newline.
    HANDSHAKE_MAX_BYTES bounds the read."""
    s, c = _socketpair()
    try:
        c.sendall(b"X" * (HANDSHAKE_MAX_BYTES * 2))
        # Give the server thread a beat to read.
        line = read_handshake(s)
        assert len(line) <= HANDSHAKE_MAX_BYTES
        assert not is_valid_handshake(line)
    finally:
        s.close()
        c.close()


def test_concurrent_handshake_does_not_deadlock():
    """Sanity: read_handshake on one socket while another connects
    shouldn't deadlock or cross-contaminate."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]

    results = []

    def handshake_one() -> None:
        conn, _ = listener.accept()
        try:
            results.append(read_handshake(conn))
        finally:
            conn.close()

    threads = [threading.Thread(target=handshake_one) for _ in range(2)]
    for t in threads:
        t.start()

    clients = []
    for _ in range(2):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.connect(("127.0.0.1", port))
        c.sendall(WIRE_MAGIC)
        clients.append(c)

    for t in threads:
        t.join(timeout=HANDSHAKE_TIMEOUT_S * 2)

    listener.close()
    for c in clients:
        c.close()

    assert results == [WIRE_MAGIC, WIRE_MAGIC]
