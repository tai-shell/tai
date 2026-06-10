"""`holo connect HOST:PORT` — stdio↔TCP bridge.

Exists so users on a fresh machine don't need `nc` to talk to a
listening `holo mcp --listen PORT`. Two threads copy bytes:
process stdin → socket, socket → process stdout. The magic
handshake prefix is injected as the first line so the user never
has to type it.

Used in `.mcp.json` like:

    holo mcp-remote -- ssh -l user host /usr/local/bin/holo connect localhost:7777

The SSH command provides the encrypted transport; `holo connect`
just bridges stdio (which `mcp-remote` is already piping through
SSH) to a local TCP connection on the remote machine.
"""

from __future__ import annotations

import socket
import sys
import threading

from holo.mcp_wire import WIRE_MAGIC


def parse_endpoint(spec: str) -> tuple[str, int]:
    """Parse `host:port` (or `[ipv6]:port`)."""
    if spec.startswith("["):
        end = spec.find("]")
        if end == -1 or end + 1 >= len(spec) or spec[end + 1] != ":":
            raise ValueError(f"invalid endpoint {spec!r}: expected [host]:port")
        host = spec[1:end]
        port_s = spec[end + 2:]
    else:
        if ":" not in spec:
            raise ValueError(f"invalid endpoint {spec!r}: expected host:port")
        host, port_s = spec.rsplit(":", 1)
    try:
        port = int(port_s)
    except ValueError as e:
        raise ValueError(f"invalid port in endpoint {spec!r}") from e
    if not (0 < port < 65536):
        raise ValueError(f"port out of range in endpoint {spec!r}")
    return host, port


def run(endpoint: str) -> int:
    """Connect to `endpoint`, write the magic prefix, then bridge
    stdio in both directions until either side closes."""
    try:
        host, port = parse_endpoint(endpoint)
    except ValueError as e:
        print(f"holo connect: {e}", file=sys.stderr)
        return 2

    try:
        conn = socket.create_connection((host, port), timeout=10.0)
    except OSError as e:
        print(f"holo connect: {host}:{port}: {e}", file=sys.stderr)
        return 1
    conn.settimeout(None)

    try:
        conn.sendall(WIRE_MAGIC)
    except OSError as e:
        print(f"holo connect: handshake send failed: {e}", file=sys.stderr)
        conn.close()
        return 1

    def stdin_to_socket() -> None:
        try:
            while True:
                chunk = sys.stdin.buffer.read1(4096)
                if not chunk:
                    break
                conn.sendall(chunk)
        except OSError:
            pass
        finally:
            # Half-close the write side so the server sees EOF on its
            # read and finishes sending whatever's queued. Inbound
            # thread keeps reading until the server hangs up.
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def socket_to_stdout() -> None:
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        except OSError:
            pass

    t1 = threading.Thread(target=stdin_to_socket, name="holo-connect-in", daemon=True)
    t2 = threading.Thread(target=socket_to_stdout, name="holo-connect-out", daemon=True)
    t1.start()
    t2.start()

    # Wait on the inbound copier — it exits when the server closes,
    # which is the canonical "we're done" signal. The outbound
    # copier may still be blocked on stdin; daemon=True lets the
    # process exit clean it up.
    t2.join()
    try:
        conn.close()
    except OSError:
        pass
    return 0
