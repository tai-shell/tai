"""Unit tests for holo.cloudcity_discover.

Pin: TXT parser (schema validation, type coercion), CloudCityStore
(upsert/remove/snapshot/fanout/stale sweep), the zeroconf
ServiceListener bridge, and the `/cloudcities` route in
``holo.discover.build_app``.

We don't open a real Zeroconf socket here — multicast on test
runners is flaky. Real-network behaviour is exercised by the manual
smoke test in PR #82's spec section.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from holo.cloudcity_announce import (
    FIELD_BACKEND,
    FIELD_CA_FPS,
    FIELD_HOST,
    FIELD_IPS,
    FIELD_PORT,
    FIELD_USER,
    FIELD_V,
    FIELD_VERSION,
    SERVICE_TYPE,
    TXT_SCHEMA_VERSION,
)
from holo.cloudcity_discover import (
    CloudCityListener,
    CloudCityStore,
    _instance_from_name,
    parse_txt,
)


def _txt(d: dict[str, str]) -> dict[bytes, bytes]:
    return {k.encode("utf-8"): v.encode("utf-8") for k, v in d.items()}


# ------------------------------------------------------------------ parser


class TestParser:
    def _valid_txt(self) -> dict[str, str]:
        return {
            FIELD_V: TXT_SCHEMA_VERSION,
            FIELD_HOST: "MacBook-Air-3.local",
            FIELD_IPS: "192.168.1.45,10.55.195.6",
            FIELD_PORT: "2222",
        }

    def test_minimal_record_parses(self) -> None:
        rec = parse_txt(_txt(self._valid_txt()), "cc-host-abc123")
        assert rec is not None
        assert rec["instance"] == "cc-host-abc123"
        assert rec[FIELD_V] == TXT_SCHEMA_VERSION
        assert rec[FIELD_HOST] == "MacBook-Air-3.local"
        assert rec[FIELD_PORT] == 2222  # coerced
        assert rec[FIELD_IPS] == ["192.168.1.45", "10.55.195.6"]
        assert "last_seen" in rec

    def test_missing_required_field_drops(self) -> None:
        bad = self._valid_txt()
        del bad[FIELD_HOST]
        assert parse_txt(_txt(bad), "cc-host-abc123") is None

    def test_unknown_schema_version_drops(self) -> None:
        bad = self._valid_txt()
        bad[FIELD_V] = "99"
        assert parse_txt(_txt(bad), "cc-host-abc123") is None

    def test_non_integer_port_drops(self) -> None:
        bad = self._valid_txt()
        bad[FIELD_PORT] = "not-a-number"
        assert parse_txt(_txt(bad), "cc-host-abc123") is None

    def test_optional_fields_present_when_advertised(self) -> None:
        d = self._valid_txt()
        d[FIELD_BACKEND] = "http://192.168.1.45:8081"
        d[FIELD_CA_FPS] = "SHA256:abc,SHA256:def"
        d[FIELD_USER] = "alice"
        d[FIELD_VERSION] = "0.39"
        rec = parse_txt(_txt(d), "cc-host-abc123")
        assert rec is not None
        assert rec[FIELD_BACKEND] == "http://192.168.1.45:8081"
        assert rec[FIELD_CA_FPS] == ["SHA256:abc", "SHA256:def"]
        assert rec[FIELD_USER] == "alice"
        assert rec[FIELD_VERSION] == "0.39"

    def test_optional_fields_omitted_when_unset(self) -> None:
        rec = parse_txt(_txt(self._valid_txt()), "cc-host-abc123")
        assert rec is not None
        assert FIELD_BACKEND not in rec
        assert FIELD_CA_FPS not in rec
        assert FIELD_USER not in rec
        assert FIELD_VERSION not in rec

    def test_non_utf8_field_drops_record(self) -> None:
        # Build a TXT record with one field that's not valid UTF-8.
        props: dict[bytes, bytes] = _txt(self._valid_txt())
        props[b"junk"] = b"\xff\xfe\x00"
        assert parse_txt(props, "cc-host-abc123") is None


def test_instance_from_name_strips_suffix() -> None:
    full = "cloudcity-MacBook-Air-3-abc123." + SERVICE_TYPE
    assert _instance_from_name(full) == "cloudcity-MacBook-Air-3-abc123"


def test_instance_from_name_passthrough_when_no_suffix() -> None:
    assert _instance_from_name("foo") == "foo"


# ------------------------------------------------------------------ store


class TestCloudCityStore:
    def _rec(self, instance: str, **extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "instance": instance,
            FIELD_V: TXT_SCHEMA_VERSION,
            FIELD_HOST: instance + ".local",
            FIELD_IPS: ["192.168.1.5"],
            FIELD_PORT: 2222,
            "last_seen": int(time.time()),
        }
        base.update(extra)
        return base

    def test_upsert_emits_add_then_update(self) -> None:
        s = CloudCityStore()
        events: list[dict[str, Any]] = []
        s.subscribe(events.append)
        e1 = s.upsert(self._rec("cc-1"))
        assert e1["type"] == "add"
        e2 = s.upsert(self._rec("cc-1", port=2223))
        assert e2["type"] == "update"
        assert events == [e1, e2]

    def test_remove_returns_event_and_emits(self) -> None:
        s = CloudCityStore()
        s.upsert(self._rec("cc-1"))
        events: list[dict[str, Any]] = []
        s.subscribe(events.append)
        ev = s.remove("cc-1")
        assert ev is not None
        assert ev == {"type": "remove", "instance": "cc-1"}
        assert events == [ev]

    def test_remove_missing_is_noop(self) -> None:
        s = CloudCityStore()
        events: list[dict[str, Any]] = []
        s.subscribe(events.append)
        assert s.remove("not-there") is None
        assert events == []

    def test_snapshot_sorted_by_instance(self) -> None:
        s = CloudCityStore()
        s.upsert(self._rec("cc-bravo"))
        s.upsert(self._rec("cc-alpha"))
        s.upsert(self._rec("cc-charlie"))
        instances = [r["instance"] for r in s.snapshot()]
        assert instances == ["cc-alpha", "cc-bravo", "cc-charlie"]

    def test_unsubscribe_stops_fanout(self) -> None:
        s = CloudCityStore()
        events: list[dict[str, Any]] = []
        token = s.subscribe(events.append)
        s.upsert(self._rec("cc-1"))
        s.unsubscribe(token)
        s.upsert(self._rec("cc-1", port=2223))
        # First upsert delivered; second should not.
        assert len(events) == 1

    def test_subscriber_exception_does_not_break_fanout(self) -> None:
        s = CloudCityStore()
        survivor_events: list[dict[str, Any]] = []

        def boom(_e: dict[str, Any]) -> None:
            raise RuntimeError("subscriber exploded")

        s.subscribe(boom)
        s.subscribe(survivor_events.append)
        s.upsert(self._rec("cc-1"))
        assert len(survivor_events) == 1

    def test_prune_stale_drops_old_entries(self) -> None:
        s = CloudCityStore()
        old = self._rec("cc-old")
        old["last_seen"] = int(time.time()) - 1000
        fresh = self._rec("cc-fresh")
        s.upsert(old)
        s.upsert(fresh)
        events: list[dict[str, Any]] = []
        s.subscribe(events.append)
        pruned = s.prune_stale(stale_after_s=150.0)
        assert pruned == 1
        # Only the old one is gone; the remove event was emitted.
        assert any(
            e.get("type") == "remove" and e.get("instance") == "cc-old"
            for e in events
        )
        assert [r["instance"] for r in s.snapshot()] == ["cc-fresh"]


# -------------------------------------------------------------- zeroconf bridge


class TestListener:
    def test_add_service_fetches_and_upserts(self) -> None:
        store = CloudCityStore()
        zc = MagicMock()
        info = MagicMock()
        info.properties = _txt(
            {
                FIELD_V: TXT_SCHEMA_VERSION,
                FIELD_HOST: "MacBook.local",
                FIELD_IPS: "192.168.1.5",
                FIELD_PORT: "2222",
            }
        )
        info.server = "MacBook.local."
        zc.get_service_info.return_value = info

        listener = CloudCityListener(zc, store)
        listener.add_service(zc, SERVICE_TYPE, "cc-host-abc." + SERVICE_TYPE)

        snap = store.snapshot()
        assert len(snap) == 1
        assert snap[0]["instance"] == "cc-host-abc"

    def test_remove_service_deletes_from_store(self) -> None:
        store = CloudCityStore()
        # Pre-seed a record so the remove has something to do.
        store.upsert(
            {
                "instance": "cc-host-abc",
                FIELD_V: TXT_SCHEMA_VERSION,
                FIELD_HOST: "x",
                FIELD_IPS: ["1.1.1.1"],
                FIELD_PORT: 2222,
                "last_seen": int(time.time()),
            }
        )
        zc = MagicMock()
        listener = CloudCityListener(zc, store)
        listener.remove_service(zc, SERVICE_TYPE, "cc-host-abc." + SERVICE_TYPE)
        assert store.snapshot() == []

    def test_get_service_info_none_is_silent(self) -> None:
        """If zeroconf returns None (race / dead announce), no upsert."""
        store = CloudCityStore()
        zc = MagicMock()
        zc.get_service_info.return_value = None
        listener = CloudCityListener(zc, store)
        listener.add_service(zc, SERVICE_TYPE, "cc-host-abc." + SERVICE_TYPE)
        assert store.snapshot() == []

    def test_invalid_txt_is_silent(self) -> None:
        """Malformed TXT (e.g. missing required field) is dropped silently."""
        store = CloudCityStore()
        zc = MagicMock()
        info = MagicMock()
        info.properties = _txt({FIELD_V: TXT_SCHEMA_VERSION})  # no host/port/ips
        info.server = "x.local."
        zc.get_service_info.return_value = info
        listener = CloudCityListener(zc, store)
        listener.add_service(zc, SERVICE_TYPE, "cc-host-abc." + SERVICE_TYPE)
        assert store.snapshot() == []


# --------------------------------------------------- HTTP /cloudcities route


@pytest.fixture
def _starlette_test_client_or_skip() -> Any:
    """Skip these tests if Starlette/httpx aren't installed in this env.

    The discover-side tests already gate on Starlette; we mirror that
    so a CI image without web deps doesn't trip these.
    """
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("starlette not installed")
    return TestClient


def test_cloudcities_endpoint_returns_snapshot(
    _starlette_test_client_or_skip: Any,
) -> None:
    """`GET /cloudcities` returns the current store snapshot as JSON."""
    from holo.discover import SessionStore, build_app

    sess_store = SessionStore()
    cc_store = CloudCityStore()
    cc_store.upsert(
        {
            "instance": "cc-host-abc",
            FIELD_V: TXT_SCHEMA_VERSION,
            FIELD_HOST: "MacBook.local",
            FIELD_IPS: ["192.168.1.5"],
            FIELD_PORT: 2222,
            "last_seen": int(time.time()),
        }
    )

    app = build_app(store=sess_store, cloudcity_store=cc_store)

    TestClient = _starlette_test_client_or_skip
    with TestClient(app) as client:
        resp = client.get("/cloudcities")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["instance"] == "cc-host-abc"
        assert data[0][FIELD_PORT] == 2222


def test_local_cloudcity_endpoint_matches_local_ip(
    _starlette_test_client_or_skip: Any,
) -> None:
    """`/local-cloudcity` returns the announcement whose IPs overlap
    with one of the local interface IPs."""
    from unittest.mock import patch

    from holo.discover import SessionStore, build_app

    sess_store = SessionStore()
    cc_store = CloudCityStore()
    cc_store.upsert(
        {
            "instance": "cloudcity-thishost-abc",
            FIELD_V: TXT_SCHEMA_VERSION,
            FIELD_HOST: "thishost.local",
            FIELD_IPS: ["192.168.1.5"],
            FIELD_PORT: 2222,
            "last_seen": int(time.time()),
        }
    )
    cc_store.upsert(
        {
            "instance": "cloudcity-otherhost-def",
            FIELD_V: TXT_SCHEMA_VERSION,
            FIELD_HOST: "otherhost.local",
            FIELD_IPS: ["192.168.1.99"],
            FIELD_PORT: 2222,
            "last_seen": int(time.time()),
        }
    )

    app = build_app(store=sess_store, cloudcity_store=cc_store)

    TestClient = _starlette_test_client_or_skip
    # Pretend "this machine" has 192.168.1.5 on its en0; that's the
    # IP the upstairs CloudCity is announcing, so /local-cloudcity
    # should return that one and ignore the office one.
    with patch(
        "holo.discover._local_ipv4_set",
        return_value=[{"127.0.0.1", "192.168.1.5"}],
    ):
        with TestClient(app) as client:
            resp = client.get("/local-cloudcity")
            assert resp.status_code == 200
            data = resp.json()
            assert data is not None
            assert data["instance"] == "cloudcity-thishost-abc"


def test_local_cloudcity_endpoint_returns_null_on_no_match(
    _starlette_test_client_or_skip: Any,
) -> None:
    """When no announced CloudCity matches a local IP, returns null
    (so the SPA can decide what to do — show an error, fall back, etc.)."""
    from unittest.mock import patch

    from holo.discover import SessionStore, build_app

    sess_store = SessionStore()
    cc_store = CloudCityStore()
    cc_store.upsert(
        {
            "instance": "cloudcity-otherhost-def",
            FIELD_V: TXT_SCHEMA_VERSION,
            FIELD_HOST: "otherhost.local",
            FIELD_IPS: ["192.168.1.99"],
            FIELD_PORT: 2222,
            "last_seen": int(time.time()),
        }
    )

    app = build_app(store=sess_store, cloudcity_store=cc_store)

    TestClient = _starlette_test_client_or_skip
    with patch(
        "holo.discover._local_ipv4_set",
        return_value=[{"127.0.0.1", "192.168.1.5"}],
    ):
        with TestClient(app) as client:
            resp = client.get("/local-cloudcity")
            assert resp.status_code == 200
            assert resp.json() is None


def test_local_cloudcity_endpoint_empty_when_no_announcements(
    _starlette_test_client_or_skip: Any,
) -> None:
    from unittest.mock import patch

    from holo.discover import SessionStore, build_app

    sess_store = SessionStore()
    cc_store = CloudCityStore()  # empty
    app = build_app(store=sess_store, cloudcity_store=cc_store)

    TestClient = _starlette_test_client_or_skip
    with patch(
        "holo.discover._local_ipv4_set",
        return_value=[{"192.168.1.5"}],
    ):
        with TestClient(app) as client:
            resp = client.get("/local-cloudcity")
            assert resp.status_code == 200
            assert resp.json() is None


def test_sessions_endpoint_still_works_with_cloudcity_extension(
    _starlette_test_client_or_skip: Any,
) -> None:
    """The Phase-2 changes don't break the existing /sessions surface."""
    from holo.announce import (
        FIELD_CWD,
        FIELD_HOLO_PID,
        FIELD_HOLO_VERSION,
        FIELD_STARTED,
    )
    from holo.announce import (
        FIELD_HOST as SESSION_HOST,
    )
    from holo.announce import (
        FIELD_USER as SESSION_USER,
    )
    from holo.announce import (
        FIELD_V as SESSION_V,
    )
    from holo.announce import (
        TXT_SCHEMA_VERSION as SESSION_V_VAL,
    )
    from holo.discover import SessionStore, build_app

    sess_store = SessionStore()
    sess_store.upsert(
        {
            "instance": "holo-host-abc",
            SESSION_V: SESSION_V_VAL,
            SESSION_HOST: "MacBook.local",
            SESSION_USER: "alice",
            FIELD_HOLO_PID: 1234,
            FIELD_HOLO_VERSION: "0.0.0",
            FIELD_STARTED: int(time.time()),
            FIELD_CWD: "/tmp",
            "last_seen": int(time.time()),
        }
    )
    cc_store = CloudCityStore()  # empty
    app = build_app(store=sess_store, cloudcity_store=cc_store)

    TestClient = _starlette_test_client_or_skip
    with TestClient(app) as client:
        sessions = client.get("/sessions").json()
        assert len(sessions) == 1
        assert sessions[0]["instance"] == "holo-host-abc"
        cloudcities = client.get("/cloudcities").json()
        assert cloudcities == []


# ----------------------------------------------------------- stale sweep thread


def test_start_stale_sweeper_drops_old_entries() -> None:
    from holo.cloudcity_discover import start_stale_sweeper

    store = CloudCityStore()
    store.upsert(
        {
            "instance": "cc-old",
            FIELD_V: TXT_SCHEMA_VERSION,
            FIELD_HOST: "x",
            FIELD_IPS: ["1.1.1.1"],
            FIELD_PORT: 2222,
            "last_seen": int(time.time()) - 1000,
        }
    )
    stop = threading.Event()
    sweeper = start_stale_sweeper(
        store, stale_after_s=150.0, stop_event=stop
    )
    # The first sweep happens almost immediately on entry to the loop.
    # Give the daemon thread a moment to run, then assert.
    time.sleep(0.2)
    stop.set()
    sweeper.join(timeout=2.0)
    assert store.snapshot() == []
