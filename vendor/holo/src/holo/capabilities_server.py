"""HTTP server exposing the host capabilities snapshot to LAN companions.

Threat model
------------
The server binds ``0.0.0.0:<random-port>`` so any host on the LAN can
reach it. We assume the LAN is mostly trusted: anyone who can snoop
the mDNS broadcast can already grab the auth token from the TXT
record. The server is hardened against ONE specific scenario — a
random web origin (`https://evil.com`) trying to fingerprint the host
via cross-origin ``fetch()``.

Two layered defenses combine to make that browser path inert:

  1. **Auth via custom header** — every ``/capabilities`` request
     must carry ``X-Holo-Caps-Token: <hex>``; mismatches return 401.
     Token is generated per-process and shipped only via the mDNS
     TXT record, not via any user-readable surface.
  2. **No CORS allow-* headers** — we never emit
     ``Access-Control-Allow-Origin``. The custom auth header forces
     a CORS preflight; the missing allow-headers response makes the
     preflight fail; the actual cross-origin ``fetch()`` never fires.
     Belt-and-suspenders.

Endpoints
---------
``GET /capabilities`` (auth required) — JSON capabilities snapshot.
``GET /healthz`` (no auth) — liveness probe; returns ``{"status": "ok"}``
and nothing fingerprintable.

Server lifecycle
----------------
``CapabilitiesServer.start()`` runs uvicorn on a daemon thread and
blocks until the listener has bound (so the announcer can include
the actual port in TXT). ``stop()`` signals uvicorn and joins the
thread. Idempotent — repeat calls are no-ops.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

from holo.capabilities import CapabilitiesProbe

if TYPE_CHECKING:
    import uvicorn  # noqa: TC004 — only used for type hints

_log = logging.getLogger(__name__)


CAPS_TOKEN_HEADER = "X-Holo-Caps-Token"
DEFAULT_BIND_HOST = "0.0.0.0"  # noqa: S104 — LAN binding is the point
DEFAULT_READY_TIMEOUT_S = 5.0


class CapabilitiesServer:
    """Lifecycle wrapper around an in-thread uvicorn server.

    Construct with a probe and (optionally) a token; ``start()`` opens
    the listener and ``stop()`` tears it down. The ``actual_port``
    property is populated once startup completes — callers (the
    announcer) need it to advertise the right port over mDNS.
    """

    def __init__(
        self,
        *,
        probe: CapabilitiesProbe,
        token: str | None = None,
        host: str = DEFAULT_BIND_HOST,
        port: int = 0,
    ) -> None:
        self.probe = probe
        # `secrets.token_urlsafe(32)` yields ~43 chars of base64url —
        # comfortably fits in a TXT field and well above the brute-
        # force threshold for any plausible attacker.
        self.token = token if token is not None else secrets.token_urlsafe(32)
        self.host = host
        self.port = port
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._actual_port: int = 0
        self._lock = threading.Lock()

    @property
    def actual_port(self) -> int:
        """Bound port. Zero until ``start()`` returns successfully."""
        return self._actual_port

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None

    def start(self, *, ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S) -> None:
        """Launch uvicorn on a daemon thread and wait for it to bind.

        Blocks up to ``ready_timeout_s`` seconds for the listener to
        come up — we need the actual port number to ship in the mDNS
        TXT record before we return control to the announcer.
        """
        import uvicorn

        with self._lock:
            if self._server is not None:
                return
            app = self._build_app()
            config = uvicorn.Config(
                app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False,
                # We're a tiny side-server; one worker is plenty.
                workers=1,
            )
            server = uvicorn.Server(config)
            # uvicorn installs SIGINT/SIGTERM handlers by default. We're
            # running on a worker thread, where signal.signal() raises
            # ValueError (it can only be called from the main thread).
            # Holo's main process owns signal handling already.
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            self._server = server
            self._thread = threading.Thread(
                target=server.run,
                daemon=True,
                name="holo-caps-server",
            )
            self._thread.start()

        # Spin until uvicorn reports started AND has socket info we can
        # read the port off of. Bounded by ready_timeout_s so a stuck
        # startup doesn't wedge the announcer.
        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            port = self._read_actual_port()
            if port:
                self._actual_port = port
                return
            time.sleep(0.02)
        # Startup timed out — tear down so the caller can decide whether
        # to retry or surface the error.
        self.stop()
        raise RuntimeError(
            f"capabilities server failed to bind {self.host}:{self.port} "
            f"within {ready_timeout_s}s"
        )

    def _read_actual_port(self) -> int:
        """Best-effort read of the bound port from uvicorn internals.

        uvicorn exposes ``Server.servers`` once startup completes — a
        list of ``asyncio.base_events.Server`` instances, each with a
        ``.sockets`` tuple. We pick the first IPv4 socket's port.
        Returns 0 if startup hasn't finished yet.
        """
        server = self._server
        if server is None or not getattr(server, "started", False):
            return 0
        servers = getattr(server, "servers", None) or []
        for srv in servers:
            for sock in getattr(srv, "sockets", ()) or ():
                try:
                    addr = sock.getsockname()
                except OSError:
                    continue
                if isinstance(addr, tuple) and len(addr) >= 2:
                    return int(addr[1])
        return 0

    def stop(self) -> None:
        """Signal uvicorn to exit and join the thread.

        Safe to call before ``start()`` and idempotent on repeat
        calls. Bounded join timeout so a misbehaving server doesn't
        block process shutdown forever.
        """
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._actual_port = 0
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=3.0)

    # ------------------------------------------------------------------ app

    def _build_app(self) -> Any:
        """Construct the Starlette ASGI app.

        Defined as an instance method (not a module-level factory) so
        the closure can capture ``self`` for the auth check without
        threading the token through extra kwargs.
        """
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def healthz(request: Any) -> Any:
            del request
            # Intentionally minimal — anything more (interfaces, host
            # name) would let an unauthenticated LAN scanner fingerprint
            # holo hosts. The /capabilities endpoint is the only place
            # that surfaces real data, and it requires the token.
            return JSONResponse({"status": "ok"})

        async def capabilities(request: Any) -> Any:
            received = request.headers.get(CAPS_TOKEN_HEADER.lower(), "")
            # Constant-time compare to dodge timing oracles. Even if a
            # tiny one — same-LAN attackers who can snoop the broadcast
            # token already have it — there's no reason to leak signal.
            if not secrets.compare_digest(received, self.token):
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                )
            return JSONResponse(self.probe.collect())

        return Starlette(
            debug=False,
            routes=[
                Route("/capabilities", capabilities, methods=["GET"]),
                Route("/healthz", healthz, methods=["GET"]),
            ],
            # NOTE: no CORSMiddleware on purpose — see module docstring.
            # Browsers will fail the preflight for the custom header,
            # which is exactly what we want.
        )


__all__ = [
    "CAPS_TOKEN_HEADER",
    "DEFAULT_BIND_HOST",
    "DEFAULT_READY_TIMEOUT_S",
    "CapabilitiesServer",
]
