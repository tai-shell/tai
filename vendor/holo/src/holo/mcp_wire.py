"""Wire-protocol helpers shared by `holo mcp --listen` and
`holo connect`.

The MCP-over-TCP transport is line-delimited JSON-RPC, the same
on-the-wire shape as the stdio transport — but framed by a magic
prefix line so a drive-by browser can't speak it.

Threat model: a malicious page loaded in the *server's* browser can
fire `fetch("http://localhost:PORT", { method: "POST", body: ... })`
at the listener. CORS blocks the response read but the request body
DOES land. Without the prefix, that body could be a valid MCP
JSON-RPC envelope and trigger a tool call. Browsers cannot control
the first bytes of a TCP connection — fetch always sends an HTTP
request line first, WebSocket sends an HTTP Upgrade. So a server
that rejects connections whose first line isn't `HOLO/1\\n` cannot
be reached from a browser. Done.

Same-user processes that aren't browsers (rogue scripts, other
shells) can still speak the prefix; defending against those would
require a token, which a same-user attacker can read from any
configured location anyway. On a single-user machine they've
already won; no additional auth in this layer.
"""

from __future__ import annotations

import socket

WIRE_VERSION = 1
WIRE_MAGIC = f"HOLO/{WIRE_VERSION}\n".encode()
HANDSHAKE_TIMEOUT_S = 5.0
HANDSHAKE_MAX_BYTES = 64  # cap so a slow-loris client can't pin the listener


def read_handshake(conn: socket.socket) -> bytes:
    """Read up to the first newline (or HANDSHAKE_MAX_BYTES, whichever
    comes first) under HANDSHAKE_TIMEOUT_S. Returns whatever was read.

    We read one byte at a time so we don't accidentally consume MCP
    payload bytes — once the prefix is validated, subsequent reads
    via `socket.makefile()` start exactly after it.
    """
    prev_timeout = conn.gettimeout()
    conn.settimeout(HANDSHAKE_TIMEOUT_S)
    buf = bytearray()
    try:
        while len(buf) < HANDSHAKE_MAX_BYTES:
            chunk = conn.recv(1)
            if not chunk:
                break
            buf.extend(chunk)
            if buf.endswith(b"\n"):
                break
        return bytes(buf)
    except OSError:
        return bytes(buf)
    finally:
        try:
            conn.settimeout(prev_timeout)
        except OSError:
            pass


def is_valid_handshake(line: bytes) -> bool:
    return line == WIRE_MAGIC
