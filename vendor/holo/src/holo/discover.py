"""mDNS / DNS-SD discovery for holo sessions.

Reference consumer of `docs/companion-spec.md`. Subscribes to the same
`_holo-session._tcp.local.` service type that `holo.announce` broadcasts
and exposes three output modes:

    holo discover --json           one-shot snapshot, JSON array, exit
    holo discover --tail           long-running JSONL event stream
    holo discover --serve PORT     HTTP + WebSocket server (default 7082)

The parser shares TXT field-name constants with `holo.announce` so the
broadcaster and the consumer can't drift. Schema-version validation is
"fail closed on `v != \"1\"`": malformed records are logged at WARNING
and dropped from the session list.

Architecture
------------
::

    Zeroconf  →  ServiceBrowser  →  HoloListener
                                          │
                                          ▼
                                    SessionStore  (thread-safe;
                                          │       subscriber fanout)
                                          ▼
                              ┌───────────┼─────────────┐
                              ▼           ▼             ▼
                          --json       --tail        --serve
                          snapshot     JSONL         Starlette + WS

The SessionStore uses a `threading.RLock`, so zeroconf's callback
thread, the stale-sweep thread, and the asyncio loop in `--serve`
mode can all touch it safely. For `--serve` we hop the queue boundary
via ``loop.call_soon_threadsafe`` since the WS handler is async.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from holo.announce import (
    FIELD_IPS,
    FIELD_STARTED,
    FIELD_TUNNEL_PORTS,
    FIELD_V,
    INT_FIELDS,
    REQUIRED_FIELDS,
    SERVICE_TYPE,
    TXT_SCHEMA_VERSION,
    parse_tunnel_ports,
)

_log = logging.getLogger(__name__)


DEFAULT_SERVE_PORT = 7082
DEFAULT_CORS_ORIGINS = (
    "http://localhost:8888",
    "https://app-dev.tai.sh",
    "https://tai.sh",
)
# Spec §2.5: drop sessions whose last_seen is older than 2× cache TTL.
# zeroconf's default service TTL is ~75s, so 150s is the natural floor.
DEFAULT_STALE_AFTER_S = 150.0
# Sweep cadence; smaller = lower drop latency, more wake-ups.
_STALE_SWEEP_INTERVAL_S = 15.0
# How long --json waits for the browser to populate before printing.
DEFAULT_JSON_WAIT_S = 3.0
# How often --serve and DiscoverHandle tear down + recreate the
# Zeroconf instance + ServiceBrowser to defend against silent browse
# stalls (python-zeroconf has been observed to stop delivering
# callbacks after long uptimes / interface flaps). The SessionStore
# is preserved across the swap so /sessions stays continuous. Set to
# 0 to disable.
DEFAULT_REBROWSE_INTERVAL_S = 300.0
# When /sessions or /cloudcities sees an empty store, kick a fresh
# rebrowse and wait this long for callbacks to populate before
# snapshotting. Sized to one zeroconf response round-trip on a
# typical LAN; longer makes the first poll slow without much benefit.
_EMPTY_CACHE_BROWSE_WAIT_S = 1.0


# --------------------------------------------------------------------- parser


def _instance_from_name(name: str) -> str:
    """Strip the trailing `._holo-session._tcp.local.` to get the instance."""
    suffix = "." + SERVICE_TYPE
    return name[: -len(suffix)] if name.endswith(suffix) else name


def parse_txt(
    properties: dict[bytes, bytes], instance: str
) -> dict[str, Any] | None:
    """Decode a TXT record into a Session dict, or return None if invalid.

    Per spec §2.4:
      - Required fields must all be present.
      - Schema version (`v`) must equal "1"; unknown majors are dropped
        with a WARNING.
      - Optional fields are omitted from the returned dict when absent
        (the desktop UI distinguishes "unset" from "set to empty").

    The TXT bytes-to-str decode is UTF-8; on decode failure the entry
    is logged and dropped — do not silently substitute lossy data.
    """
    decoded: dict[str, str] = {}
    for raw_key, raw_val in properties.items():
        try:
            key = raw_key.decode("utf-8")
            val = (raw_val or b"").decode("utf-8")
        except UnicodeDecodeError:
            _log.warning(
                "discover: %s: dropping non-UTF-8 TXT field; bytes=%r=%r",
                instance,
                raw_key,
                raw_val,
            )
            return None
        decoded[key] = val

    missing = [f for f in REQUIRED_FIELDS if f not in decoded]
    if missing:
        _log.warning(
            "discover: %s: missing required TXT fields %s; dropping",
            instance,
            missing,
        )
        return None

    if decoded[FIELD_V] != TXT_SCHEMA_VERSION:
        _log.warning(
            "discover: %s: unsupported schema v=%r (this build understands "
            "v=%r); dropping",
            instance,
            decoded[FIELD_V],
            TXT_SCHEMA_VERSION,
        )
        return None

    session: dict[str, Any] = {"instance": instance}
    for k, v in decoded.items():
        if k in INT_FIELDS:
            try:
                session[k] = int(v)
            except ValueError:
                _log.warning(
                    "discover: %s: non-integer %s=%r; dropping", instance, k, v
                )
                return None
        elif k == FIELD_IPS:
            # Comma-separated → list[str]. Empty entries are dropped.
            session[k] = [x.strip() for x in v.split(",") if x.strip()]
        elif k == FIELD_TUNNEL_PORTS:
            # `<instance>:<port>,...` → {instance: port}. Malformed
            # entries are dropped with a WARNING but the rest of the
            # map is preserved (see ``announce.parse_tunnel_ports``).
            session[k] = parse_tunnel_ports(v)
        else:
            session[k] = v

    session["last_seen"] = int(time.time())
    return session


# --------------------------------------------------------- session store + events


class SessionStore:
    """Thread-safe map of instance → Session, plus subscriber fanout.

    Multiple writers (zeroconf callback thread, stale-sweep thread) and
    multiple readers (HTTP handler, --tail printer, WS clients) share
    one `RLock`. Subscribers receive every state change as an `Event`
    dict; they're called *while the lock is held* so subscribers must
    not block — for async consumers use ``loop.call_soon_threadsafe``
    inside the callback.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []

    def upsert(self, session: dict[str, Any]) -> dict[str, Any]:
        """Insert or replace a session by instance.

        Emits ``{"type": "add", "session": ...}`` for new instances and
        ``{"type": "update", "session": ...}`` for existing ones.
        Returns the event that was emitted.
        """
        instance = session["instance"]
        with self._lock:
            existing = self._sessions.get(instance)
            self._sessions[instance] = session
            event_type = "update" if existing is not None else "add"
            event = {"type": event_type, "session": session}
            self._fanout(event)
        return event

    def remove(self, instance: str) -> dict[str, Any] | None:
        with self._lock:
            existing = self._sessions.pop(instance, None)
            if existing is None:
                return None
            event = {"type": "remove", "instance": instance}
            self._fanout(event)
            return event

    def snapshot(self) -> list[dict[str, Any]]:
        """Return all sessions sorted by `started` ascending (oldest first).

        Stable ordering matters for UI lists — without it, every poll
        could show sessions in a different order even when nothing
        actually changed.
        """
        with self._lock:
            return sorted(
                (dict(s) for s in self._sessions.values()),
                key=lambda s: s.get(FIELD_STARTED, 0),
            )

    def subscribe(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[dict[str, Any]], None]:
        """Register a callback. Returns the same callable as a token for unsubscribe."""
        with self._lock:
            self._subscribers.append(callback)
        return callback

    def unsubscribe(self, token: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(token)
            except ValueError:
                pass

    def prune_stale(self, stale_after_s: float, now: float | None = None) -> int:
        """Drop sessions whose ``last_seen`` is older than ``stale_after_s``.

        Returns the count of pruned entries. Each removal emits a
        ``remove`` event so subscribers see them.
        """
        if now is None:
            now = time.time()
        cutoff = now - stale_after_s
        pruned = 0
        with self._lock:
            stale = [
                inst
                for inst, s in self._sessions.items()
                if s.get("last_seen", 0) < cutoff
            ]
            for inst in stale:
                if self.remove(inst) is not None:
                    pruned += 1
        return pruned

    def _fanout(self, event: dict[str, Any]) -> None:
        # Snapshot subscribers under the lock so unsubscribe during dispatch
        # doesn't mutate the list mid-iteration.
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:  # noqa: BLE001 — subscriber bugs must not kill fanout
                _log.exception("discover: subscriber raised; continuing")


# ------------------------------------------------------------ zeroconf bridge


class HoloListener:
    """Adapt python-zeroconf's `ServiceListener` callbacks to a SessionStore.

    The library invokes our methods on its own thread; we delegate
    everything heavy to the store (which is thread-safe) and never
    block in the callback path beyond a TXT lookup.
    """

    def __init__(self, zeroconf: Any, store: SessionStore) -> None:
        self._zeroconf = zeroconf
        self._store = store

    def add_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._fetch_and_upsert(zeroconf, service_type, name)

    def update_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._fetch_and_upsert(zeroconf, service_type, name)

    def remove_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        instance = _instance_from_name(name)
        self._store.remove(instance)

    def _fetch_and_upsert(
        self, zeroconf: Any, service_type: str, name: str
    ) -> None:
        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        if info is None:
            return
        instance = _instance_from_name(name)
        properties = info.properties or {}
        session = parse_txt(properties, instance)
        if session is None:
            return
        self._store.upsert(session)


def _start_browser(
    store: SessionStore | None = None,
) -> tuple[Any, Any, SessionStore]:
    """Spin up a Zeroconf instance + ServiceBrowser bound to a store.

    Pass an existing ``store`` to preserve session state across a
    rebrowse swap; omit it to allocate a fresh store. Callers are
    responsible for calling ``zeroconf.close()`` to tear it all down.
    """
    from zeroconf import ServiceBrowser, Zeroconf

    if store is None:
        store = SessionStore()
    zc = Zeroconf()
    listener = HoloListener(zc, store)
    browser = ServiceBrowser(zc, SERVICE_TYPE, listener=listener)
    return zc, browser, store


class DiscoverHandle:
    """Long-lived zeroconf browser + SessionStore + stale-sweeper bundle.

    Lets a long-running process keep a continuously-fresh view of the
    LAN broadcasts without spawning a new zeroconf browser for every
    query. The intended owner is `HoloMCPServer`: a connected agent
    can call `holo_discover_sessions` / `holo_fetch_capabilities`
    repeatedly and get instant answers because the cache is already
    populated by the time it's queried.

    Idempotent — repeat ``start()``/``stop()`` are no-ops, and
    ``stop()`` is safe to call before ``start()``. Stale sweep runs
    on the same schedule as ``--tail`` mode (default 150 s, 2× the
    zeroconf TTL) so crashed announcers eventually fall out of the
    cache without us having to plumb anything per-call.
    """

    def __init__(
        self,
        *,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        rebrowse_interval_s: float = DEFAULT_REBROWSE_INTERVAL_S,
    ) -> None:
        self._stale_after_s = stale_after_s
        self._rebrowse_interval_s = rebrowse_interval_s
        self._zc: Any = None
        self._browser: Any = None
        self._store: SessionStore | None = None
        self._stop_event: threading.Event | None = None
        self._sweeper: threading.Thread | None = None
        self._rebrowser: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._zc is not None:
                return
            self._zc, self._browser, self._store = _start_browser()
            self._stop_event = threading.Event()
            self._sweeper = _start_stale_sweeper(
                self._store, self._stale_after_s, self._stop_event
            )
            if self._rebrowse_interval_s > 0:
                self._rebrowser = _start_rebrowse_thread(
                    self, self._rebrowse_interval_s, self._stop_event
                )

    def stop(self) -> None:
        # Snapshot under the lock so concurrent stop() calls don't
        # double-close. The actual close + join happen outside the lock
        # so we don't hold it across a 2-second sweeper.join().
        with self._lock:
            stop_event = self._stop_event
            sweeper = self._sweeper
            rebrowser = self._rebrowser
            zc = self._zc
            self._stop_event = None
            self._sweeper = None
            self._rebrowser = None
            self._zc = None
            self._browser = None
            self._store = None
        if stop_event is not None:
            stop_event.set()
        if sweeper is not None:
            sweeper.join(timeout=2.0)
        if rebrowser is not None:
            rebrowser.join(timeout=2.0)
        if zc is not None:
            try:
                zc.close()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                _log.exception("DiscoverHandle: zeroconf close failed")

    def _swap_browser(self) -> None:
        """Swap to a fresh Zeroconf+ServiceBrowser, preserving the store.

        Called by the periodic rebrowse thread. Caller is responsible
        for serialization — we take ``self._lock`` here so a concurrent
        ``stop()`` can't yank the store out from under us.
        """
        with self._lock:
            if self._zc is None or self._store is None:
                return  # stop() already torn us down
            new_zc, new_browser, _ = _start_browser(store=self._store)
            old_zc = self._zc
            self._zc = new_zc
            self._browser = new_browser
        try:
            old_zc.close()
        except Exception:  # noqa: BLE001 — best-effort
            _log.exception("DiscoverHandle: rebrowse close failed")

    @property
    def is_running(self) -> bool:
        return self._zc is not None

    @property
    def store(self) -> SessionStore | None:
        return self._store

    def snapshot(self) -> list[dict[str, Any]]:
        """Return the current session list, or [] if not started."""
        store = self._store
        if store is None:
            return []
        return store.snapshot()


# -------------------------------------------------------------- mode adapters


def run_oneshot(wait_s: float = DEFAULT_JSON_WAIT_S) -> int:
    """`--json` mode: browse for `wait_s`, print snapshot, exit 0."""
    zc, _browser, store = _start_browser()
    try:
        time.sleep(wait_s)
        snapshot = store.snapshot()
    finally:
        zc.close()
    json.dump(snapshot, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def run_tail(stale_after_s: float = DEFAULT_STALE_AFTER_S) -> int:
    """`--tail` mode: stream JSONL events to stdout until SIGINT/SIGTERM."""
    from holo.mcp_server import _sigterm_as_keyboard_interrupt

    zc, _browser, store = _start_browser()
    stop_event = threading.Event()

    def emit(event: dict[str, Any]) -> None:
        try:
            sys.stdout.write(json.dumps(event) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            stop_event.set()

    # Initial snapshot as a series of `add` events so consumers don't need
    # a separate "fetch then subscribe" handshake. Subscribe first so we
    # don't miss anything that lands between the snapshot and the subscribe.
    store.subscribe(emit)
    for s in store.snapshot():
        emit({"type": "add", "session": s})

    sweeper = _start_stale_sweeper(store, stale_after_s, stop_event)

    try:
        with _sigterm_as_keyboard_interrupt():
            while not stop_event.is_set():
                stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        sweeper.join(timeout=2.0)
        zc.close()
    return 0


def run_serve(
    port: int = DEFAULT_SERVE_PORT,
    cors_origins: list[str] | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    host: str = "127.0.0.1",
    rebrowse_interval_s: float = DEFAULT_REBROWSE_INTERVAL_S,
) -> int:
    """`--serve` mode: localhost HTTP+WS service on `port`."""
    import uvicorn

    app = build_app(
        cors_origins=cors_origins,
        stale_after_s=stale_after_s,
        rebrowse_interval_s=rebrowse_interval_s,
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    print(
        f"holo discover: serving on http://{host}:{port} "
        f"(routes: /sessions /cloudcities /local-cloudcity /healthz "
        f"/dispatch /dispatch/release /events /control)",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


# -------------------------------------------------- rebrowse self-healing


def _swap_browser_sync(
    state: dict[str, Any],
    *,
    store_key: str,
    zc_key: str,
    browser_key: str,
    start_fn: Callable[..., tuple[Any, Any, Any]],
) -> None:
    """Tear down the old Zeroconf+ServiceBrowser and start a fresh pair.

    The existing store at ``state[store_key]`` is preserved across
    the swap, so ``/sessions`` (or any other consumer) sees no gap.
    Synchronous because python-zeroconf I/O is synchronous; callers
    in async contexts must run this in a thread pool.
    """
    new_zc, new_browser, _ = start_fn(store=state[store_key])
    old_zc = state[zc_key]
    state[zc_key] = new_zc
    state[browser_key] = new_browser
    if old_zc is not None:
        try:
            old_zc.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            _log.exception("rebrowse swap: failed to close old zeroconf")


async def _rebrowse_loop(
    state: dict[str, Any],
    *,
    interval_s: float,
    stop_event: asyncio.Event,
    store_key: str,
    zc_key: str,
    browser_key: str,
    start_fn: Callable[..., tuple[Any, Any, Any]],
) -> None:
    """Periodically swap the Zeroconf browser bound to ``state[store_key]``.

    No-op when ``interval_s <= 0``. Returns when ``stop_event`` is
    set. Swap exceptions are logged and the loop continues — a
    single bad swap shouldn't kill self-healing forever.
    """
    if interval_s <= 0:
        await stop_event.wait()
        return
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            return  # stop fired during the wait
        except TimeoutError:
            pass
        try:
            await loop.run_in_executor(
                None,
                lambda: _swap_browser_sync(
                    state,
                    store_key=store_key,
                    zc_key=zc_key,
                    browser_key=browser_key,
                    start_fn=start_fn,
                ),
            )
        except Exception:  # noqa: BLE001 — keep the loop alive
            _log.exception("rebrowse loop: swap failed (continuing)")


async def _ensure_populated(
    state: dict[str, Any],
    lock: asyncio.Lock,
    *,
    store_key: str,
    zc_key: str,
    browser_key: str,
    start_fn: Callable[..., tuple[Any, Any, Any]],
    wait_s: float = _EMPTY_CACHE_BROWSE_WAIT_S,
) -> None:
    """Empty-cache safety net: kick a rebrowse + brief wait when empty.

    If ``state[store_key]`` is non-empty this returns immediately —
    the periodic rebrowse keeps the store fresh on the happy path.
    When the store is empty (e.g. between startup and the first
    arriving announce, or after a silent browse stall the periodic
    rebrowse hasn't reached yet) we swap to a fresh Zeroconf+browser
    and wait ``wait_s`` for callbacks to populate before returning.
    A single lock serializes concurrent callers so they share one
    rebrowse rather than stampeding the zeroconf socket.
    """
    if state[store_key].snapshot():
        return
    async with lock:
        if state[store_key].snapshot():
            return  # another caller raced ahead and refreshed
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: _swap_browser_sync(
                    state,
                    store_key=store_key,
                    zc_key=zc_key,
                    browser_key=browser_key,
                    start_fn=start_fn,
                ),
            )
            await asyncio.sleep(wait_s)
        except Exception:  # noqa: BLE001 — best-effort
            _log.exception("empty-cache fallback: refresh failed")


# ----------------------------------------------------------------- stale sweep


def _start_stale_sweeper(
    store: SessionStore, stale_after_s: float, stop_event: threading.Event
) -> threading.Thread:
    """Launch a daemon thread that prunes stale sessions periodically."""

    def loop() -> None:
        while not stop_event.is_set():
            try:
                store.prune_stale(stale_after_s)
            except Exception:  # noqa: BLE001 — log and keep sweeping
                _log.exception("discover: stale sweep raised")
            stop_event.wait(timeout=_STALE_SWEEP_INTERVAL_S)

    t = threading.Thread(
        target=loop, name="holo-discover-sweeper", daemon=True
    )
    t.start()
    return t


def _start_rebrowse_thread(
    handle: DiscoverHandle,
    interval_s: float,
    stop_event: threading.Event,
) -> threading.Thread:
    """Launch a daemon thread that periodically rebrowses on a handle.

    Sync analog of ``_rebrowse_loop`` for ``DiscoverHandle``. Same
    rationale: python-zeroconf has been seen to silently stop
    delivering callbacks after long uptimes, so we swap the browser
    on a timer and keep the existing store.
    """

    def loop() -> None:
        while not stop_event.is_set():
            stop_event.wait(timeout=interval_s)
            if stop_event.is_set():
                return
            try:
                handle._swap_browser()
            except Exception:  # noqa: BLE001 — keep the loop alive
                _log.exception("DiscoverHandle rebrowse: swap failed")

    t = threading.Thread(
        target=loop, name="holo-discover-rebrowser", daemon=True
    )
    t.start()
    return t


# ------------------------------------------------------------------ HTTP+WS app


def build_app(
    *,
    store: SessionStore | None = None,
    cloudcity_store: Any = None,
    cors_origins: list[str] | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    rebrowse_interval_s: float = DEFAULT_REBROWSE_INTERVAL_S,
) -> Any:
    """Construct the Starlette ASGI app for `--serve`.

    Hosts both the holo-session discoverer and the CloudCity
    discoverer in a single process — one Starlette app, two zeroconf
    browsers, two stale-sweep threads. The CloudCity surface is the
    Phase-2 addition from the reverse-tunnel spec
    (docs/holo-cloudcity-tunnel-spec.md §4.2).

    Stores may be passed in for tests; production callers leave them
    None so the app spins up its own browsers at startup.
    """
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    # Lazy-import so test paths that pass an explicit `cloudcity_store`
    # don't pay the import cost — and so test_discover.py tests that
    # touch only the session paths don't need cloudcity_discover loaded.
    from holo import cloudcity_discover
    from holo import dispatch as _dispatch

    origins = list(cors_origins) if cors_origins else list(DEFAULT_CORS_ORIGINS)

    state: dict[str, Any] = {
        "store": store,
        "zc": None,
        "browser": None,
        "stop_event": threading.Event(),
        "sweeper": None,
        "rebrowse_stop": None,  # asyncio.Event (created in lifespan)
        "rebrowse_task": None,  # asyncio.Task
        "rebrowse_lock": None,  # asyncio.Lock (created in lifespan)
        "cc_store": cloudcity_store,
        "cc_zc": None,
        "cc_browser": None,
        "cc_stop_event": threading.Event(),
        "cc_sweeper": None,
        "cc_rebrowse_stop": None,
        "cc_rebrowse_task": None,
        "cc_rebrowse_lock": None,
        "dispatch": _dispatch.DispatchState(),
    }

    async def sessions_endpoint(request: Any) -> Any:
        del request
        await _ensure_populated(
            state,
            state["rebrowse_lock"],
            store_key="store",
            zc_key="zc",
            browser_key="browser",
            start_fn=_start_browser,
        )
        return JSONResponse(state["store"].snapshot())

    async def cloudcities_endpoint(request: Any) -> Any:
        del request
        await _ensure_populated(
            state,
            state["cc_rebrowse_lock"],
            store_key="cc_store",
            zc_key="cc_zc",
            browser_key="cc_browser",
            start_fn=cloudcity_discover._start_browser,
        )
        return JSONResponse(state["cc_store"].snapshot())

    async def local_cloudcity_endpoint(request: Any) -> Any:
        """Identify which discovered CloudCity is on this machine.

        Phase 5b: when a Host B daemon tunnels into multiple
        CloudCities (one per workstation), the session announce
        carries a `tunnel_ports={instance: port, ...}` map. Each SPA
        needs to look up the entry whose key is *its own* CloudCity.
        That lookup needs an instance label; this endpoint returns it
        (along with the full record) by matching announced IPs
        against the local interface IPs.

        Returns the matching record, or `null` when:
          - no CloudCity is announced from this machine yet
          - no `_cloudcity._tcp.local.` records have been observed
          - all matching candidates are stale (sweeper hasn't run yet)

        The match is "any announced IP overlaps with any local
        interface IPv4". Picks the first hit in snapshot order; in
        practice there's only one CloudCity per machine, so the
        ambiguity rarely matters.
        """
        del request
        local_ips = {
            ip
            for adapter in _local_ipv4_set()
            for ip in adapter
        }
        for record in state["cc_store"].snapshot():
            announced = record.get("ips") or []
            if any(ip in local_ips for ip in announced):
                return JSONResponse(record)
        return JSONResponse(None)

    async def healthz_endpoint(request: Any) -> Any:
        del request
        return JSONResponse(
            {
                "status": "ok",
                "interfaces": _list_interfaces(),
                "zt_present": _zt_present(),
            }
        )

    async def events_ws(websocket: Any) -> None:
        await websocket.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def forward(event: dict[str, Any]) -> None:
            # Hop from zeroconf's thread to the asyncio loop.
            loop.call_soon_threadsafe(queue.put_nowait, event)

        token = state["store"].subscribe(forward)
        try:
            for s in state["store"].snapshot():
                await websocket.send_json({"type": "add", "session": s})
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except Exception:
            # Most often: client disconnected. Don't log every WS close
            # as an error.
            pass
        finally:
            state["store"].unsubscribe(token)
            try:
                await websocket.close()
            except Exception:
                pass

    @asynccontextmanager
    async def lifespan(app):  # type: ignore[no-untyped-def]
        del app
        if state["store"] is None:
            zc, browser, store_ = _start_browser()
            state["store"] = store_
            state["zc"] = zc
            state["browser"] = browser
            state["sweeper"] = _start_stale_sweeper(
                store_, stale_after_s, state["stop_event"]
            )
        if state["cc_store"] is None:
            cc_zc, cc_browser, cc_store_ = cloudcity_discover._start_browser()
            state["cc_store"] = cc_store_
            state["cc_zc"] = cc_zc
            state["cc_browser"] = cc_browser
            state["cc_sweeper"] = cloudcity_discover.start_stale_sweeper(
                cc_store_, stale_after_s, state["cc_stop_event"]
            )

        # Self-healing rebrowse: periodically swap each Zeroconf
        # browser bound to a stable store. asyncio.Event/Lock must be
        # created inside the running loop, not at module import time.
        state["rebrowse_lock"] = asyncio.Lock()
        state["cc_rebrowse_lock"] = asyncio.Lock()
        state["rebrowse_stop"] = asyncio.Event()
        state["cc_rebrowse_stop"] = asyncio.Event()
        state["rebrowse_task"] = asyncio.create_task(
            _rebrowse_loop(
                state,
                interval_s=rebrowse_interval_s,
                stop_event=state["rebrowse_stop"],
                store_key="store",
                zc_key="zc",
                browser_key="browser",
                start_fn=_start_browser,
            ),
            name="holo-discover-rebrowse",
        )
        state["cc_rebrowse_task"] = asyncio.create_task(
            _rebrowse_loop(
                state,
                interval_s=rebrowse_interval_s,
                stop_event=state["cc_rebrowse_stop"],
                store_key="cc_store",
                zc_key="cc_zc",
                browser_key="cc_browser",
                start_fn=cloudcity_discover._start_browser,
            ),
            name="holo-discover-cc-rebrowse",
        )

        try:
            yield
        finally:
            # Stop async rebrowse tasks first (they may be inside a
            # run_in_executor swap; let them finish before we close
            # zc handles out from under them).
            for stop_key, task_key in (
                ("rebrowse_stop", "rebrowse_task"),
                ("cc_rebrowse_stop", "cc_rebrowse_task"),
            ):
                stop = state.get(stop_key)
                task = state.get(task_key)
                if stop is not None:
                    stop.set()
                if task is not None:
                    try:
                        await asyncio.wait_for(task, timeout=3.0)
                    except Exception:  # noqa: BLE001 — timeout or task error, cancel either way
                        task.cancel()
            state["stop_event"].set()
            state["cc_stop_event"].set()
            if state["sweeper"] is not None:
                state["sweeper"].join(timeout=2.0)
            if state["cc_sweeper"] is not None:
                state["cc_sweeper"].join(timeout=2.0)
            if state["zc"] is not None:
                state["zc"].close()
            if state["cc_zc"] is not None:
                state["cc_zc"].close()

    # tai dispatch routes — see docs/dispatch-protocol.md in the
    # tai-shell/tai repo. Selector matching needs read-only access to
    # the announce store; we pass a snapshot fn so dispatch.py never
    # imports the discover internals.
    dispatch_state = state["dispatch"]
    sessions_snapshot = lambda: state["store"].snapshot()  # noqa: E731
    control_ws = _dispatch.make_control_ws(dispatch_state, sessions_snapshot)
    dispatch_endpoint = _dispatch.make_dispatch_endpoint(
        dispatch_state, sessions_snapshot
    )
    release_endpoint = _dispatch.make_release_endpoint(dispatch_state)

    return Starlette(
        debug=False,
        routes=[
            Route("/sessions", sessions_endpoint, methods=["GET"]),
            Route("/cloudcities", cloudcities_endpoint, methods=["GET"]),
            Route(
                "/local-cloudcity",
                local_cloudcity_endpoint,
                methods=["GET"],
            ),
            Route("/healthz", healthz_endpoint, methods=["GET"]),
            Route("/dispatch", dispatch_endpoint, methods=["POST"]),
            Route(
                "/dispatch/release",
                release_endpoint,
                methods=["POST"],
            ),
            WebSocketRoute("/events", events_ws),
            WebSocketRoute("/control", control_ws),
        ],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_methods=["GET", "POST"],
                allow_headers=["*"],
            ),
        ],
        lifespan=lifespan,
    )


# --------------------------------------------------------------- /healthz info


def _list_interfaces() -> list[dict[str, Any]]:
    """Enumerate local interfaces with their IPv4 addresses for /healthz."""
    try:
        import ifaddr
    except ImportError:
        return []
    out: list[dict[str, Any]] = []
    for adapter in ifaddr.get_adapters():
        ipv4: list[str] = []
        for ip in adapter.ips:
            if isinstance(ip.ip, str):
                ipv4.append(ip.ip)
        out.append({"name": adapter.name, "ipv4": ipv4})
    return out


def _local_ipv4_set() -> list[set[str]]:
    """Return per-adapter sets of local IPv4 addresses.

    Used by /local-cloudcity to match announced CloudCity IPs against
    interfaces this machine actually owns. Per-adapter grouping lets
    us short-circuit on a partial match without flattening the whole
    interface table into a single set (which we then have to flatten
    in the consumer anyway, but the per-adapter shape leaves room
    for "match against this specific interface only" later).
    """
    try:
        import ifaddr
    except ImportError:
        return []
    out: list[set[str]] = []
    for adapter in ifaddr.get_adapters():
        ips = {ip.ip for ip in adapter.ips if isinstance(ip.ip, str)}
        if ips:
            out.append(ips)
    return out


def _zt_present() -> bool:
    """Best-effort detection of a ZeroTier interface.

    macOS/Linux name interfaces `ztXXXX` (10-char ZT id with `zt` prefix)
    or `feth*` on some macOS configurations; we look for `zt` prefix as
    the common case. Windows ZT uses adapter descriptions rather than
    name prefixes — for v1 we only try the prefix heuristic.
    """
    try:
        import ifaddr
    except ImportError:
        return False
    for adapter in ifaddr.get_adapters():
        name = (adapter.name or "").lower()
        if name.startswith("zt"):
            return True
    return False


# ---------------------- helpful when invoking the module manually ----------------


def main(args: list[str]) -> int:
    """Dispatcher for `holo discover`. CLI parsing lives in `holo.cli`."""
    raise NotImplementedError(
        "Call holo.discover.run_oneshot/run_tail/run_serve directly; "
        "argument parsing happens in holo.cli."
    )


__all__ = [
    "DEFAULT_CORS_ORIGINS",
    "DEFAULT_JSON_WAIT_S",
    "DEFAULT_SERVE_PORT",
    "DEFAULT_STALE_AFTER_S",
    "DiscoverHandle",
    "HoloListener",
    "SessionStore",
    "build_app",
    "parse_txt",
    "run_oneshot",
    "run_serve",
    "run_tail",
]
