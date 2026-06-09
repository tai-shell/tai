"""Auto-tunnel watcher (Phase 5b of the CloudCity tunnel spec).

Watches `_cloudcity._tcp.local.` broadcasts and proactively maintains
one reverse SSH tunnel per visible CloudCity. The point: a single
holo daemon can be reachable from multiple desktops at once — one
upstairs, one in the office, one on a colleague's laptop, etc. —
and each desktop's SPA picks its own tunnel out of the daemon's
``tunnel_ports`` announce map.

Topology (from the spec, restated for orientation):

    [Host B: this daemon]              [CloudCity-A: workstation 1]
                                            sshd :2222
       holo daemon ──┬── ssh -A -N -R 0:localhost:22 ──► loopback:P_A
                     │
                     │                  [CloudCity-B: workstation 2]
                     │                       sshd :2222
                     └── ssh -A -N -R 0:localhost:22 ──► loopback:P_B

Each tunnel lives in its own CloudCity's loopback scope, so P_A and
P_B are allocated independently and may differ. The session announce
carries `tunnel_ports=cc-A:P_A,cc-B:P_B`; SPAs at each desktop look
up the entry whose key matches their local CloudCity.

Lifecycle:

    auto = AutoTunnel(announcer=announcer, backend=...)
    auto.start()
    ...
    auto.stop()  # stops all tunnels + clears tunnel_ports announce

Design notes:

- A single worker thread drains a queue of CloudCity events. The
  zeroconf callback thread enqueues; we never block multicast on
  slow ssh handshakes. Events are processed in arrival order.
- ``add`` / ``update`` for a new cc → open tunnel + register port.
- ``add`` / ``update`` for a known cc → no-op (refresh of TXT, IP set
  may have changed but we don't rebuild — limitation; would need a
  reachability probe).
- ``remove`` for a known cc → stop the corresponding tunnel.
- After every change, the announcer's ``set_tunnel_ports`` is called
  with the current map so listening SPAs see the change immediately.
- No reconnect-with-backoff in v1 — if an ssh subprocess dies, we
  drop the entry and wait for the next mDNS event for that CloudCity
  to retrigger. mDNS TTL refresh worst case ~75s.
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Any

from holo import cert as cert_mod
from holo import tunnel as tunnel_mod

_log = logging.getLogger(__name__)


class AutoTunnel:
    """Maintains one reverse-tunnel per visible CloudCity, lifecycle-bound.

    Construct with the announcer the daemon is using (so we can update
    its ``tunnel_ports`` map) and the cert backend URL. Call
    ``start()`` once; tunnels come up as CloudCities are discovered.
    Call ``stop()`` to tear everything down.
    """

    def __init__(
        self,
        *,
        announcer: Any | None,
        backend: str | None = None,
        key_path: Path = cert_mod.DEFAULT_KEY_PATH,
        principal: str | None = None,
    ) -> None:
        self.announcer = announcer
        self.backend = backend
        self.key_path = key_path
        self.principal = principal or tunnel_mod.parse_principal_from_env()

        # instance → {"tunnel": Tunnel, "record": dict}
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

        # Wired up at start().
        self._zc: Any = None
        self._browser: Any = None
        self._store: Any = None
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue()

    # ------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Spin up the CloudCity browser + worker thread."""
        if self._zc is not None:
            return  # idempotent

        from holo import cloudcity_discover

        self._zc, self._browser, self._store = cloudcity_discover._start_browser()
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._run, name="holo-auto-tunnel", daemon=True
        )
        self._worker.start()

        # Subscribe BEFORE replaying the snapshot so we don't miss any
        # broadcasts that arrive between snapshot() and subscribe().
        self._store.subscribe(self._enqueue)
        for cc in self._store.snapshot():
            self._event_queue.put({"type": "add", "cloudcity": cc})

    def stop(self) -> None:
        """Tear down all tunnels + the browser. Idempotent."""
        if self._zc is None and self._worker is None:
            return

        self._stop_event.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None

        with self._lock:
            instances = list(self._entries.keys())
        for inst in instances:
            self._tear_down(inst)

        if self.announcer is not None:
            try:
                self.announcer.set_tunnel_ports(None)
            except Exception:  # noqa: BLE001 — shutdown must not raise
                _log.exception("auto_tunnel: clearing tunnel_ports failed")

        if self._zc is not None:
            try:
                self._zc.close()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                _log.exception("auto_tunnel: zeroconf close failed")
            self._zc = None
            self._browser = None
            self._store = None

    @property
    def is_running(self) -> bool:
        return self._zc is not None

    def snapshot(self) -> dict[str, int]:
        """Return ``{cloudcity_instance: tunnel_port}`` for active tunnels.

        Useful for tests and for diagnostics endpoints.
        """
        with self._lock:
            return {
                inst: e["tunnel"].port
                for inst, e in self._entries.items()
                if e.get("tunnel") is not None
                and e["tunnel"].port is not None
            }

    # ----------------------------------------------------- event pump

    def _enqueue(self, event: dict[str, Any]) -> None:
        """zeroconf callback hops events onto our worker's queue."""
        if self._stop_event.is_set():
            return
        self._event_queue.put(event)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                event = self._event_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle_event(event)
            except Exception:  # noqa: BLE001 — keep the worker alive
                _log.exception("auto_tunnel: event handling crashed")

    def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype in ("add", "update"):
            cc = event.get("cloudcity")
            if cc:
                self._ensure_tunnel(cc)
        elif etype == "remove":
            inst = event.get("instance")
            if inst:
                self._tear_down(inst)
        self._republish_map()

    # ------------------------------------------------------ tunnel ops

    def _ensure_tunnel(self, cc_record: dict[str, Any]) -> None:
        inst = cc_record.get("instance")
        if not inst:
            return
        with self._lock:
            existing = self._entries.get(inst)
        if existing is not None:
            # We already have a tunnel for this CloudCity. Refresh
            # the cached record (so a re-broadcast with new metadata
            # — e.g. a tweaked backend URL — doesn't lose state) but
            # don't rebuild the SSH subprocess: the underlying
            # connection is still valid.
            existing["record"] = cc_record
            return

        # Pick the right backend: the CloudCity's announced backend
        # wins if set (per-CloudCity CA), else our configured
        # fallback. Lets a fleet of CloudCities each issue their own
        # certs — relevant when CAs are scoped per-environment.
        backend = self.backend or cc_record.get("backend")

        try:
            t = tunnel_mod.open_to_cloudcity(
                cc_record,
                backend=backend,
                key_path=self.key_path,
                principal=self.principal,
            )
        except Exception as e:  # noqa: BLE001 — surface + continue
            _log.warning(
                "auto_tunnel: failed to open tunnel to %s: %s", inst, e
            )
            return

        with self._lock:
            self._entries[inst] = {"tunnel": t, "record": cc_record}
        _log.info(
            "auto_tunnel: opened tunnel to %s on port %s", inst, t.port
        )

    def _tear_down(self, instance: str) -> None:
        with self._lock:
            entry = self._entries.pop(instance, None)
        if entry is None:
            return
        t = entry.get("tunnel")
        if t is not None:
            try:
                t.stop()
            except Exception:  # noqa: BLE001 — log + continue
                _log.exception(
                    "auto_tunnel: tearing down tunnel to %s failed", instance
                )
        _log.info("auto_tunnel: torn down tunnel to %s", instance)

    def _republish_map(self) -> None:
        """Push the current ``{instance: port}`` map onto the announcer.

        Called after every event so listeners see the change without
        waiting for an mDNS TTL refresh.
        """
        if self.announcer is None:
            return
        ports = self.snapshot()
        try:
            self.announcer.set_tunnel_ports(ports or None)
        except Exception:  # noqa: BLE001 — log + continue
            _log.exception("auto_tunnel: republish failed")


__all__ = ["AutoTunnel"]
