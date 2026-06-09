"""mDNS / DNS-SD discovery for CloudCity hosts.

Reference consumer of the CloudCity announce schema defined in
``holo.cloudcity_announce`` (Phase 1) and the spec at
https://github.com/bradclarkalexander/desktop/blob/develop/docs/holo-cloudcity-tunnel-spec.md
§3.2 / §4.2. Subscribes to the same ``_cloudcity._tcp.local.``
service type that ``CloudCityAnnouncer`` broadcasts.

Two output modes (matching the holo-session ``discover`` shape):

    holo cloudcity discover --json    one-shot snapshot, JSON array, exit
    holo cloudcity discover --tail    long-running JSONL event stream

The HTTP/WS surface is co-hosted inside the existing
``holo discover --serve PORT`` daemon — see ``holo.discover.build_app``,
which wires a CloudCity browser alongside the session browser and
exposes ``GET /cloudcities`` returning the snapshot. There is no
separate ``--serve`` mode here on purpose: holo daemons that already
care about ``_holo-session._tcp.`` discovery typically also want
CloudCity discovery, and one Starlette app + one stale-sweep thread
per process is cheaper than two.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from holo.cloudcity_announce import (
    FIELD_HOST,
    FIELD_IPS,
    FIELD_V,
    INT_FIELDS,
    REQUIRED_FIELDS,
    SERVICE_TYPE,
    TXT_SCHEMA_VERSION,
)

_log = logging.getLogger(__name__)


# Drop CloudCities whose last_seen is older than this. Matches the
# session discoverer's default — same zeroconf TTL applies to both.
DEFAULT_STALE_AFTER_S = 150.0
_STALE_SWEEP_INTERVAL_S = 15.0
DEFAULT_JSON_WAIT_S = 3.0


# --------------------------------------------------------------------- parser


def _instance_from_name(name: str) -> str:
    """Strip the trailing `._cloudcity._tcp.local.` to get the instance."""
    suffix = "." + SERVICE_TYPE
    return name[: -len(suffix)] if name.endswith(suffix) else name


def parse_txt(
    properties: dict[bytes, bytes], instance: str
) -> dict[str, Any] | None:
    """Decode a CloudCity TXT record, or return ``None`` if invalid.

    Mirrors ``holo.discover.parse_txt``:
      - All :data:`REQUIRED_FIELDS` must be present.
      - Schema version must equal :data:`TXT_SCHEMA_VERSION`; mismatches
        are logged at WARNING and dropped (forward-incompat record).
      - Optional fields are omitted from the returned dict when absent
        so consumers can distinguish "unset" from "empty".

    The returned dict has the shape that ``GET /cloudcities`` will
    serve to the desktop SPA: parsed types (port as int, ips as list),
    plus an ``instance`` and ``last_seen`` we add ourselves.
    """
    decoded: dict[str, str] = {}
    for raw_key, raw_val in properties.items():
        try:
            key = raw_key.decode("utf-8")
            val = (raw_val or b"").decode("utf-8")
        except UnicodeDecodeError:
            _log.warning(
                "cloudcity-discover: %s: dropping non-UTF-8 TXT field; "
                "bytes=%r=%r",
                instance,
                raw_key,
                raw_val,
            )
            return None
        decoded[key] = val

    missing = [f for f in REQUIRED_FIELDS if f not in decoded]
    if missing:
        _log.warning(
            "cloudcity-discover: %s: missing required TXT fields %s; dropping",
            instance,
            missing,
        )
        return None

    if decoded[FIELD_V] != TXT_SCHEMA_VERSION:
        _log.warning(
            "cloudcity-discover: %s: unsupported schema v=%r (this build "
            "understands v=%r); dropping",
            instance,
            decoded[FIELD_V],
            TXT_SCHEMA_VERSION,
        )
        return None

    record: dict[str, Any] = {"instance": instance}
    for k, v in decoded.items():
        if k in INT_FIELDS:
            try:
                record[k] = int(v)
            except ValueError:
                _log.warning(
                    "cloudcity-discover: %s: non-integer %s=%r; dropping",
                    instance,
                    k,
                    v,
                )
                return None
        elif k == FIELD_IPS:
            record[k] = [x.strip() for x in v.split(",") if x.strip()]
        elif k == "ca_fps":
            # Comma-separated like ips; treat the same way for a clean
            # client API. Empty means "no fingerprints advertised", not
            # "fingerprints aren't supported".
            record[k] = [x.strip() for x in v.split(",") if x.strip()]
        else:
            record[k] = v

    record["last_seen"] = int(time.time())
    return record


# --------------------------------------------------------- store + events


class CloudCityStore:
    """Thread-safe map of instance → CloudCity record, with subscriber fanout.

    Same shape as ``holo.discover.SessionStore`` but typed for the
    CloudCity record schema (no ``started`` field, so snapshot ordering
    is by instance label for stable UI rendering).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        instance = record["instance"]
        with self._lock:
            existing = self._records.get(instance)
            self._records[instance] = record
            event_type = "update" if existing is not None else "add"
            event = {"type": event_type, "cloudcity": record}
            self._fanout(event)
        return event

    def remove(self, instance: str) -> dict[str, Any] | None:
        with self._lock:
            existing = self._records.pop(instance, None)
            if existing is None:
                return None
            event = {"type": "remove", "instance": instance}
            self._fanout(event)
            return event

    def snapshot(self) -> list[dict[str, Any]]:
        """Return all records sorted by instance label (stable for UIs)."""
        with self._lock:
            return sorted(
                (dict(r) for r in self._records.values()),
                key=lambda r: r.get("instance", ""),
            )

    def subscribe(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[dict[str, Any]], None]:
        with self._lock:
            self._subscribers.append(callback)
        return callback

    def unsubscribe(self, token: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(token)
            except ValueError:
                pass

    def prune_stale(
        self, stale_after_s: float, now: float | None = None
    ) -> int:
        if now is None:
            now = time.time()
        cutoff = now - stale_after_s
        pruned = 0
        with self._lock:
            stale = [
                inst
                for inst, r in self._records.items()
                if r.get("last_seen", 0) < cutoff
            ]
            for inst in stale:
                if self.remove(inst) is not None:
                    pruned += 1
        return pruned

    def _fanout(self, event: dict[str, Any]) -> None:
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:  # noqa: BLE001 — subscriber bugs must not kill fanout
                _log.exception(
                    "cloudcity-discover: subscriber raised; continuing"
                )


# ------------------------------------------------------------ zeroconf bridge


class CloudCityListener:
    """Adapt python-zeroconf's ``ServiceListener`` callbacks to a store.

    Mirrors ``holo.discover.HoloListener`` for the CloudCity service.
    """

    def __init__(self, zeroconf: Any, store: CloudCityStore) -> None:
        self._zeroconf = zeroconf
        self._store = store

    def add_service(
        self, zeroconf: Any, service_type: str, name: str
    ) -> None:
        self._fetch_and_upsert(zeroconf, service_type, name)

    def update_service(
        self, zeroconf: Any, service_type: str, name: str
    ) -> None:
        self._fetch_and_upsert(zeroconf, service_type, name)

    def remove_service(
        self, zeroconf: Any, service_type: str, name: str
    ) -> None:
        del zeroconf, service_type
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
        record = parse_txt(properties, instance)
        if record is None:
            return
        # Belt + suspenders: if the announcer sent `host` but it's empty,
        # fall back to whatever zeroconf resolved as the server name so
        # the UI never sees a blank label.
        if not record.get(FIELD_HOST) and getattr(info, "server", None):
            record[FIELD_HOST] = info.server.rstrip(".")
        self._store.upsert(record)


def _start_browser(
    store: CloudCityStore | None = None,
) -> tuple[Any, Any, CloudCityStore]:
    """Spin up a Zeroconf browser bound to a CloudCityStore.

    Pass an existing ``store`` to preserve state across a rebrowse
    swap (see ``holo.discover`` for the swap pattern); omit to
    allocate a fresh store. Caller owns ``zc.close()``.
    """
    from zeroconf import ServiceBrowser, Zeroconf

    if store is None:
        store = CloudCityStore()
    zc = Zeroconf()
    listener = CloudCityListener(zc, store)
    browser = ServiceBrowser(zc, SERVICE_TYPE, listener=listener)
    return zc, browser, store


# -------------------------------------------------------------- mode adapters


def run_oneshot(wait_s: float = DEFAULT_JSON_WAIT_S) -> int:
    """``--json`` mode: browse for ``wait_s``, print snapshot, exit 0."""
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
    """``--tail`` mode: stream JSONL events to stdout until SIGINT/SIGTERM.

    Initial snapshot is emitted as a series of ``add`` events so a
    consumer doesn't need a separate "fetch then subscribe" handshake.
    """
    from holo.mcp_server import _sigterm_as_keyboard_interrupt

    zc, _browser, store = _start_browser()
    stop_event = threading.Event()

    def emit(event: dict[str, Any]) -> None:
        try:
            sys.stdout.write(json.dumps(event) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            stop_event.set()

    store.subscribe(emit)
    for r in store.snapshot():
        emit({"type": "add", "cloudcity": r})

    sweeper = start_stale_sweeper(store, stale_after_s, stop_event)

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


# ----------------------------------------------------------------- stale sweep


def start_stale_sweeper(
    store: CloudCityStore,
    stale_after_s: float,
    stop_event: threading.Event,
) -> threading.Thread:
    """Launch a daemon thread that prunes stale CloudCities periodically.

    Public so ``holo.discover.build_app`` can hook it into its
    co-hosted lifespan without re-implementing the sweep loop.
    """

    def loop() -> None:
        while not stop_event.is_set():
            try:
                store.prune_stale(stale_after_s)
            except Exception:  # noqa: BLE001 — log and keep sweeping
                _log.exception("cloudcity-discover: stale sweep raised")
            stop_event.wait(timeout=_STALE_SWEEP_INTERVAL_S)

    t = threading.Thread(
        target=loop, name="holo-cloudcity-sweeper", daemon=True
    )
    t.start()
    return t


__all__ = [
    "DEFAULT_JSON_WAIT_S",
    "DEFAULT_STALE_AFTER_S",
    "CloudCityListener",
    "CloudCityStore",
    "_start_browser",
    "parse_txt",
    "run_oneshot",
    "run_tail",
    "start_stale_sweeper",
]
