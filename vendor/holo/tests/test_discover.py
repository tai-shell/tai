"""Unit tests for holo.discover.

Exercise the parser (schema validation, optional-field handling), the
SessionStore (upsert/remove/snapshot/fanout/stale sweep), the
zeroconf ServiceListener bridge, the CLI flag parsing, and the
Starlette HTTP+WS app via TestClient.

We don't open a real Zeroconf socket here — multicast on test
runners is flaky. Real-network behaviour is exercised by the manual
smoke test described in `docs/companion-spec.md` §7 and by the live
e2e checks in the PR description.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from holo.announce import (
    FIELD_CWD,
    FIELD_HOLO_PID,
    FIELD_HOLO_VERSION,
    FIELD_HOST,
    FIELD_IPS,
    FIELD_SESSION,
    FIELD_SSH_USER,
    FIELD_STARTED,
    FIELD_TMUX_SESSION,
    FIELD_USER,
    FIELD_V,
)
from holo.cloudcity_discover import CloudCityStore
from holo.discover import (
    DEFAULT_CORS_ORIGINS,
    DEFAULT_JSON_WAIT_S,
    DEFAULT_REBROWSE_INTERVAL_S,
    DEFAULT_SERVE_PORT,
    DEFAULT_STALE_AFTER_S,
    HoloListener,
    SessionStore,
    _instance_from_name,
    build_app,
    parse_txt,
)


def _txt(**kwargs: str) -> dict[bytes, bytes]:
    """Build a TXT-record dict with required defaults filled in."""
    base = {
        FIELD_V: "1",
        FIELD_HOST: "test-host.local",
        FIELD_USER: "alice",
        FIELD_HOLO_PID: "12345",
        FIELD_HOLO_VERSION: "0.1.0a12",
        FIELD_STARTED: "1700000000",
        FIELD_CWD: "/work",
    }
    base.update(kwargs)
    return {k.encode(): v.encode() for k, v in base.items()}


# --------------------------------------------------------------------- parser


class TestParseTxt:
    def test_required_fields_produce_session(self) -> None:
        s = parse_txt(_txt(), "holo-test-1")
        assert s is not None
        assert s["instance"] == "holo-test-1"
        assert s[FIELD_V] == "1"
        assert s[FIELD_HOST] == "test-host.local"
        assert s[FIELD_USER] == "alice"
        assert s[FIELD_HOLO_PID] == 12345
        assert s[FIELD_STARTED] == 1700000000
        assert s[FIELD_CWD] == "/work"
        assert "last_seen" in s

    def test_missing_required_field_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        props = _txt()
        del props[FIELD_HOST.encode()]
        with caplog.at_level("WARNING"):
            s = parse_txt(props, "holo-test-1")
        assert s is None
        assert any("missing required" in r.message for r in caplog.records)

    def test_unknown_schema_version_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            s = parse_txt(_txt(v="2"), "holo-test-1")
        assert s is None
        assert any("unsupported schema" in r.message for r in caplog.records)

    def test_optional_fields_included_when_present(self) -> None:
        s = parse_txt(
            _txt(
                ssh_user="bob",
                session="claude-1",
                tmux_session="claude-1",
                tmux_window="0",
            ),
            "holo-x",
        )
        assert s is not None
        assert s[FIELD_SSH_USER] == "bob"
        assert s[FIELD_SESSION] == "claude-1"
        assert s[FIELD_TMUX_SESSION] == "claude-1"

    def test_optional_fields_excluded_when_absent(self) -> None:
        s = parse_txt(_txt(), "holo-x")
        assert s is not None
        assert FIELD_SSH_USER not in s
        assert FIELD_SESSION not in s
        assert FIELD_TMUX_SESSION not in s

    def test_ips_split_on_comma(self) -> None:
        s = parse_txt(_txt(ips="10.0.0.1,192.168.1.5"), "holo-x")
        assert s is not None
        assert s[FIELD_IPS] == ["10.0.0.1", "192.168.1.5"]

    def test_ips_strips_whitespace_and_empty(self) -> None:
        s = parse_txt(_txt(ips="10.0.0.1, ,192.168.1.5,"), "holo-x")
        assert s is not None
        assert s[FIELD_IPS] == ["10.0.0.1", "192.168.1.5"]

    def test_integer_fields_converted(self) -> None:
        s = parse_txt(_txt(holo_pid="999", started="42"), "holo-x")
        assert s is not None
        assert s[FIELD_HOLO_PID] == 999
        assert s[FIELD_STARTED] == 42
        assert isinstance(s[FIELD_HOLO_PID], int)
        assert isinstance(s[FIELD_STARTED], int)

    def test_tunnel_ports_decoded_as_dict(self) -> None:
        """Phase 5b: `tunnel_ports=A:N,B:M` parses to {A: N, B: M}."""
        from holo.announce import FIELD_TUNNEL_PORTS

        s = parse_txt(
            _txt(tunnel_ports="cc-upstairs:51492,cc-office:51493"),
            "holo-x",
        )
        assert s is not None
        assert s[FIELD_TUNNEL_PORTS] == {
            "cc-upstairs": 51492,
            "cc-office": 51493,
        }

    def test_tunnel_ports_absent_omitted_from_record(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORTS

        s = parse_txt(_txt(), "holo-x")
        assert s is not None
        assert FIELD_TUNNEL_PORTS not in s

    def test_non_integer_in_int_field_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            s = parse_txt(_txt(holo_pid="not-an-int"), "holo-x")
        assert s is None
        assert any("non-integer" in r.message for r in caplog.records)

    def test_non_utf8_bytes_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        props = _txt()
        # \xff alone is invalid UTF-8.
        props[b"v"] = b"\xff\xfe"
        with caplog.at_level("WARNING"):
            s = parse_txt(props, "holo-x")
        assert s is None

    def test_instance_from_name(self) -> None:
        assert (
            _instance_from_name(
                "holo-foo-1234-abc._holo-session._tcp.local."
            )
            == "holo-foo-1234-abc"
        )

    def test_instance_from_name_passthrough_on_malformed(self) -> None:
        # Defensive: if zeroconf hands us a name without the suffix, keep
        # the raw value rather than trim something unrelated.
        assert _instance_from_name("strange-name") == "strange-name"


# --------------------------------------------------------------- session store


class TestSessionStore:
    def _add(self, store: SessionStore, instance: str, **extra: Any) -> None:
        s = parse_txt(_txt(**extra), instance)
        assert s is not None
        store.upsert(s)

    def test_upsert_emits_add(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        store.subscribe(events.append)
        self._add(store, "holo-1")
        assert events[-1]["type"] == "add"
        assert events[-1]["session"]["instance"] == "holo-1"

    def test_upsert_existing_emits_update(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        self._add(store, "holo-1")  # subscribe AFTER initial add
        store.subscribe(events.append)
        self._add(store, "holo-1", session="changed")
        assert events == [
            {"type": "update", "session": events[0]["session"]}
        ]

    def test_remove_emits_remove(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        self._add(store, "holo-1")
        store.subscribe(events.append)
        result = store.remove("holo-1")
        assert result == {"type": "remove", "instance": "holo-1"}
        assert events == [{"type": "remove", "instance": "holo-1"}]

    def test_remove_nonexistent_returns_none_and_emits_nothing(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        store.subscribe(events.append)
        assert store.remove("nope") is None
        assert events == []

    def test_snapshot_sorted_by_started(self) -> None:
        store = SessionStore()
        self._add(store, "holo-young", started="2000")
        self._add(store, "holo-old", started="1000")
        self._add(store, "holo-mid", started="1500")
        snap = store.snapshot()
        assert [s["instance"] for s in snap] == ["holo-old", "holo-mid", "holo-young"]

    def test_unsubscribe_stops_callback(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        token = store.subscribe(events.append)
        self._add(store, "holo-1")
        store.unsubscribe(token)
        self._add(store, "holo-2")
        assert len(events) == 1

    def test_subscriber_exception_does_not_break_fanout(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = SessionStore()

        def bad(_event: dict[str, Any]) -> None:
            raise RuntimeError("boom")

        good_events: list[dict[str, Any]] = []
        store.subscribe(bad)
        store.subscribe(good_events.append)
        with caplog.at_level("ERROR"):
            self._add(store, "holo-1")
        assert len(good_events) == 1
        assert any("subscriber raised" in r.message for r in caplog.records)

    def test_prune_stale_drops_old_entries(self) -> None:
        store = SessionStore()
        s1 = parse_txt(_txt(), "holo-old")
        s2 = parse_txt(_txt(session="x"), "holo-fresh")
        assert s1 is not None and s2 is not None
        s1["last_seen"] = 1000
        s2["last_seen"] = 9000
        store.upsert(s1)
        store.upsert(s2)

        events: list[dict[str, Any]] = []
        store.subscribe(events.append)
        pruned = store.prune_stale(stale_after_s=100, now=9050)
        assert pruned == 1
        assert {s["instance"] for s in store.snapshot()} == {"holo-fresh"}
        assert events == [{"type": "remove", "instance": "holo-old"}]

    def test_concurrent_upsert_does_not_lose_subscribers(self) -> None:
        # Smoke: hammer the store from multiple threads, verify the
        # subscriber list survives intact.
        store = SessionStore()
        events: list[dict[str, Any]] = []
        store.subscribe(events.append)

        def worker(start: int) -> None:
            for i in range(20):
                s = parse_txt(_txt(), f"inst-{start}-{i}")
                if s is not None:
                    store.upsert(s)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(events) == 80


# ------------------------------------------------------------ zeroconf bridge


class TestHoloListener:
    def test_add_service_fetches_and_upserts(self) -> None:
        store = SessionStore()
        zc = MagicMock()
        info = MagicMock()
        info.properties = _txt()
        zc.get_service_info.return_value = info

        listener = HoloListener(zc, store)
        listener.add_service(
            zc,
            "_holo-session._tcp.local.",
            "holo-x._holo-session._tcp.local.",
        )

        snap = store.snapshot()
        assert len(snap) == 1
        assert snap[0]["instance"] == "holo-x"

    def test_add_service_drops_when_lookup_returns_none(self) -> None:
        store = SessionStore()
        zc = MagicMock()
        zc.get_service_info.return_value = None
        HoloListener(zc, store).add_service(
            zc, "_holo-session._tcp.local.", "holo-x._holo-session._tcp.local."
        )
        assert store.snapshot() == []

    def test_remove_service_removes_by_instance(self) -> None:
        store = SessionStore()
        s = parse_txt(_txt(), "holo-x")
        assert s is not None
        store.upsert(s)
        zc = MagicMock()
        HoloListener(zc, store).remove_service(
            zc, "_holo-session._tcp.local.", "holo-x._holo-session._tcp.local."
        )
        assert store.snapshot() == []

    def test_update_service_updates_in_place(self) -> None:
        store = SessionStore()
        zc = MagicMock()
        info = MagicMock()
        info.properties = _txt()
        zc.get_service_info.return_value = info
        listener = HoloListener(zc, store)

        listener.add_service(
            zc, "_holo-session._tcp.local.", "holo-x._holo-session._tcp.local."
        )
        # Mutate the TXT and update.
        info.properties = _txt(session="changed")
        listener.update_service(
            zc, "_holo-session._tcp.local.", "holo-x._holo-session._tcp.local."
        )
        snap = store.snapshot()
        assert len(snap) == 1
        assert snap[0][FIELD_SESSION] == "changed"


# ----------------------------------------------------------------- CLI parsing


class TestCLIDiscover:
    def test_no_mode_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        from holo.cli import main

        rc = main(["discover"])
        assert rc == 2
        assert "pick exactly one mode" in capsys.readouterr().err

    def test_multiple_modes_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["discover", "--json", "--tail"])
        assert rc == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_serve_without_port_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["discover", "--serve"])
        assert rc == 2
        assert "--serve requires" in capsys.readouterr().err

    def test_serve_invalid_port(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["discover", "--serve", "not-a-port"])
        assert rc == 2

    def test_json_calls_oneshot_with_default_wait(self) -> None:
        from holo.cli import main

        with patch("holo.discover.run_oneshot") as run_oneshot:
            run_oneshot.return_value = 0
            rc = main(["discover", "--json"])
        assert rc == 0
        run_oneshot.assert_called_once_with(wait_s=DEFAULT_JSON_WAIT_S)

    def test_json_passes_explicit_wait(self) -> None:
        from holo.cli import main

        with patch("holo.discover.run_oneshot") as run_oneshot:
            run_oneshot.return_value = 0
            main(["discover", "--json", "--wait", "10"])
        run_oneshot.assert_called_once_with(wait_s=10.0)

    def test_tail_calls_run_tail(self) -> None:
        from holo.cli import main

        with patch("holo.discover.run_tail") as run_tail:
            run_tail.return_value = 0
            main(["discover", "--tail", "--stale-after", "60"])
        run_tail.assert_called_once_with(stale_after_s=60.0)

    def test_serve_calls_run_serve_with_cors(self) -> None:
        from holo.cli import main

        with patch("holo.discover.run_serve") as run_serve:
            run_serve.return_value = 0
            main(
                [
                    "discover",
                    "--serve",
                    "9000",
                    "--cors-origin",
                    "https://a.test, https://b.test",
                ]
            )
        run_serve.assert_called_once_with(
            port=9000,
            cors_origins=["https://a.test", "https://b.test"],
            stale_after_s=DEFAULT_STALE_AFTER_S,
            rebrowse_interval_s=DEFAULT_REBROWSE_INTERVAL_S,
        )

    def test_default_serve_port_is_7082(self) -> None:
        # Sanity check: keep the constant aligned with the brief.
        assert DEFAULT_SERVE_PORT == 7082

    def test_default_cors_origins(self) -> None:
        # Sanity check: keep the dev-time defaults aligned with the brief.
        assert "http://localhost:8888" in DEFAULT_CORS_ORIGINS
        assert "https://app-dev.tai.sh" in DEFAULT_CORS_ORIGINS
        assert "https://tai.sh" in DEFAULT_CORS_ORIGINS


# ------------------------------------------------------------------ HTTP + WS


class TestHTTPApp:
    """Drive the Starlette app via TestClient — no real zeroconf."""

    def test_sessions_endpoint_returns_snapshot(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        s = parse_txt(_txt(session="claude-1"), "holo-x")
        assert s is not None
        store.upsert(s)
        app = build_app(store=store, cloudcity_store=CloudCityStore())
        with TestClient(app) as client:
            resp = client.get("/sessions")
            assert resp.status_code == 200
            body = resp.json()
            assert len(body) == 1
            assert body[0]["instance"] == "holo-x"
            assert body[0][FIELD_SESSION] == "claude-1"

    def test_healthz_shape(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        app = build_app(store=store, cloudcity_store=CloudCityStore())
        with TestClient(app) as client:
            resp = client.get("/healthz")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert isinstance(body["interfaces"], list)
            assert isinstance(body["zt_present"], bool)

    def test_events_ws_initial_snapshot(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        s = parse_txt(_txt(), "holo-x")
        assert s is not None
        store.upsert(s)

        app = build_app(store=store, cloudcity_store=CloudCityStore())
        with TestClient(app) as client:
            with client.websocket_connect("/events") as ws:
                event = ws.receive_json()
                assert event["type"] == "add"
                assert event["session"]["instance"] == "holo-x"

    def test_cors_allow_origins_default(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        app = build_app(store=store, cloudcity_store=CloudCityStore())
        with TestClient(app) as client:
            resp = client.get(
                "/sessions",
                headers={"origin": "http://localhost:8888"},
            )
            assert resp.headers.get("access-control-allow-origin") == (
                "http://localhost:8888"
            )

    def test_cors_origin_override(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        app = build_app(
            store=store,
            cloudcity_store=CloudCityStore(),
            cors_origins=["https://only-this.test"],
        )
        with TestClient(app) as client:
            resp = client.get(
                "/sessions",
                headers={"origin": "https://only-this.test"},
            )
            assert resp.headers.get("access-control-allow-origin") == (
                "https://only-this.test"
            )


# ============================================================================
# DiscoverHandle — long-lived browser used by HoloMCPServer
# ============================================================================


class TestDiscoverHandle:
    """`DiscoverHandle` bundles `_start_browser` + `_start_stale_sweeper`
    behind a start/stop lifecycle so HoloMCPServer can keep one running
    for the lifetime of the MCP session."""

    def _stub_zc(self) -> object:
        # Minimal stand-in — we never let the real zeroconf socket open.
        class _Z:
            def __init__(self) -> None:
                self.close_called = False

            def close(self) -> None:
                self.close_called = True

        return _Z()

    def test_start_calls_start_browser_once(self) -> None:
        from unittest.mock import MagicMock, patch

        from holo.discover import DiscoverHandle, SessionStore

        store = SessionStore()
        with (
            patch(
                "holo.discover._start_browser",
                return_value=(self._stub_zc(), None, store),
            ) as start_browser,
            patch(
                "holo.discover._start_stale_sweeper",
                return_value=MagicMock(),
            ),
        ):
            h = DiscoverHandle()
            h.start()
            try:
                assert start_browser.call_count == 1
                # Repeated start must be a no-op.
                h.start()
                assert start_browser.call_count == 1
            finally:
                h.stop()

    def test_snapshot_reflects_store(self) -> None:
        from unittest.mock import MagicMock, patch

        from holo.discover import DiscoverHandle, SessionStore

        store = SessionStore()
        store.upsert(
            {
                "instance": "holo-x-1-aaa",
                "host": "host.local",
                "user": "alice",
                "v": "1",
                "holo_pid": 1,
                "holo_version": "0.1.0a16",
                "started": 1_700_000_000,
                "cwd": "/x",
                "last_seen": 1_700_000_001,
            }
        )
        with (
            patch(
                "holo.discover._start_browser",
                return_value=(self._stub_zc(), None, store),
            ),
            patch(
                "holo.discover._start_stale_sweeper",
                return_value=MagicMock(),
            ),
        ):
            h = DiscoverHandle()
            h.start()
            try:
                snap = h.snapshot()
            finally:
                h.stop()
        assert len(snap) == 1
        assert snap[0]["instance"] == "holo-x-1-aaa"

    def test_snapshot_before_start_returns_empty(self) -> None:
        from holo.discover import DiscoverHandle

        # Cleanly handles "queried before started" — we want callers
        # to get [] rather than an exception so the MCP tool can
        # degrade gracefully.
        h = DiscoverHandle()
        assert h.snapshot() == []

    def test_stop_before_start_is_noop(self) -> None:
        from holo.discover import DiscoverHandle

        DiscoverHandle().stop()  # must not raise

    def test_stop_closes_zeroconf_and_signals_sweeper(self) -> None:
        from unittest.mock import patch

        from holo.discover import DiscoverHandle, SessionStore

        store = SessionStore()
        zc = self._stub_zc()

        # Capture the stop_event so we can verify it was set.
        captured: dict[str, Any] = {}

        def fake_sweeper(store_, stale_after_s, stop_event):  # noqa: ARG001
            import threading

            captured["stop_event"] = stop_event
            t = threading.Thread(
                target=lambda: stop_event.wait(timeout=5.0),
                daemon=True,
            )
            t.start()
            return t

        with (
            patch(
                "holo.discover._start_browser",
                return_value=(zc, None, store),
            ),
            patch(
                "holo.discover._start_stale_sweeper",
                side_effect=fake_sweeper,
            ),
        ):
            h = DiscoverHandle()
            h.start()
            h.stop()

        assert zc.close_called is True
        assert captured["stop_event"].is_set()


# ============================================================================
# Self-healing rebrowse — periodic swap + empty-cache fallback
# ============================================================================


class TestSwapBrowserSync:
    """The sync swap helper: tear down the old zc, install a fresh one,
    keep the existing store across the swap so /sessions stays continuous."""

    def _stub_zc(self) -> object:
        class _Z:
            def __init__(self) -> None:
                self.close_called = False
                self.close_should_raise = False

            def close(self) -> None:
                self.close_called = True
                if self.close_should_raise:
                    raise RuntimeError("simulated close failure")

        return _Z()

    def test_preserves_store_across_swap(self) -> None:
        from holo.discover import SessionStore, _swap_browser_sync

        store = SessionStore()
        old_zc = self._stub_zc()
        new_zc = self._stub_zc()
        captured_store: dict[str, Any] = {}

        def fake_start(store=None):  # noqa: ANN001
            captured_store["arg"] = store
            return (new_zc, "new-browser", store)

        state: dict[str, Any] = {
            "store": store,
            "zc": old_zc,
            "browser": "old-browser",
        }
        _swap_browser_sync(
            state,
            store_key="store",
            zc_key="zc",
            browser_key="browser",
            start_fn=fake_start,
        )

        # The same store instance went into the new browser.
        assert captured_store["arg"] is store
        # State holds the new pair, old zc was closed.
        assert state["zc"] is new_zc
        assert state["browser"] == "new-browser"
        assert old_zc.close_called is True

    def test_close_failure_does_not_raise(self) -> None:
        # If the old zc throws on close, the swap must still complete
        # — otherwise a single bad close kills self-healing forever.
        from holo.discover import SessionStore, _swap_browser_sync

        store = SessionStore()
        old_zc = self._stub_zc()
        old_zc.close_should_raise = True
        new_zc = self._stub_zc()

        def fake_start(store=None):  # noqa: ANN001
            return (new_zc, "new-browser", store)

        state: dict[str, Any] = {
            "store": store,
            "zc": old_zc,
            "browser": "old-browser",
        }
        _swap_browser_sync(  # must not raise
            state,
            store_key="store",
            zc_key="zc",
            browser_key="browser",
            start_fn=fake_start,
        )
        assert state["zc"] is new_zc


class TestRebrowseLoopAsync:
    """The asyncio rebrowse loop used by build_app lifespan."""

    def test_zero_interval_blocks_until_stop_no_swap(self) -> None:
        import asyncio

        from holo.discover import _rebrowse_loop

        call_count = {"n": 0}

        def fake_start(store=None):  # noqa: ANN001
            call_count["n"] += 1
            return (object(), object(), store)

        async def go() -> None:
            stop = asyncio.Event()
            state: dict[str, Any] = {"store": object(), "zc": None, "browser": None}
            task = asyncio.create_task(
                _rebrowse_loop(
                    state,
                    interval_s=0,
                    stop_event=stop,
                    store_key="store",
                    zc_key="zc",
                    browser_key="browser",
                    start_fn=fake_start,
                )
            )
            # Give the loop a moment, then stop. No swap should fire.
            await asyncio.sleep(0.05)
            stop.set()
            await asyncio.wait_for(task, timeout=1.0)

        asyncio.run(go())
        assert call_count["n"] == 0

    def test_fires_one_swap_per_interval(self) -> None:
        import asyncio

        from holo.discover import SessionStore, _rebrowse_loop

        store = SessionStore()
        swap_count = {"n": 0}

        def fake_start(store=None):  # noqa: ANN001
            swap_count["n"] += 1

            class _Z:
                def close(self) -> None:
                    pass

            return (_Z(), object(), store)

        async def go() -> None:
            stop = asyncio.Event()
            state: dict[str, Any] = {
                "store": store,
                "zc": None,
                "browser": None,
            }
            task = asyncio.create_task(
                _rebrowse_loop(
                    state,
                    interval_s=0.05,  # 50ms — fast for the test
                    stop_event=stop,
                    store_key="store",
                    zc_key="zc",
                    browser_key="browser",
                    start_fn=fake_start,
                )
            )
            await asyncio.sleep(0.18)  # ≈ 3 intervals
            stop.set()
            await asyncio.wait_for(task, timeout=1.0)

        asyncio.run(go())
        # 3 ± 1 swaps. Exact count is timing-dependent; require ≥ 2 so
        # the test fails closed if the loop never runs.
        assert swap_count["n"] >= 2


class TestEnsurePopulated:
    """Empty-cache safety net: kick a rebrowse + brief wait when empty."""

    def test_no_op_when_store_non_empty(self) -> None:
        import asyncio

        from holo.discover import SessionStore, _ensure_populated

        store = SessionStore()
        s = parse_txt(_txt(), "holo-x")
        assert s is not None
        store.upsert(s)

        called = {"n": 0}

        def fake_start(store=None):  # noqa: ANN001
            called["n"] += 1
            return (object(), object(), store)

        async def go() -> None:
            state: dict[str, Any] = {"store": store, "zc": None, "browser": None}
            await _ensure_populated(
                state,
                asyncio.Lock(),
                store_key="store",
                zc_key="zc",
                browser_key="browser",
                start_fn=fake_start,
                wait_s=0.0,
            )

        asyncio.run(go())
        assert called["n"] == 0

    def test_swaps_when_store_empty(self) -> None:
        import asyncio

        from holo.discover import SessionStore, _ensure_populated

        store = SessionStore()
        new_zc = type(
            "_Z", (), {"close": lambda self: None}
        )()

        def fake_start(store=None):  # noqa: ANN001
            # Simulate a record arriving via the new browser's listener.
            s = parse_txt(_txt(session="post-rebrowse"), "holo-post")
            assert s is not None
            store.upsert(s)
            return (new_zc, object(), store)

        async def go() -> None:
            state: dict[str, Any] = {"store": store, "zc": None, "browser": None}
            await _ensure_populated(
                state,
                asyncio.Lock(),
                store_key="store",
                zc_key="zc",
                browser_key="browser",
                start_fn=fake_start,
                wait_s=0.0,
            )

        asyncio.run(go())
        snap = store.snapshot()
        assert len(snap) == 1
        assert snap[0]["instance"] == "holo-post"


class TestDiscoverHandleRebrowse:
    """DiscoverHandle: periodic swap via daemon thread."""

    def _stub_zc(self) -> object:
        class _Z:
            def __init__(self) -> None:
                self.close_called = False

            def close(self) -> None:
                self.close_called = True

        return _Z()

    def test_zero_interval_starts_no_rebrowse_thread(self) -> None:
        from unittest.mock import patch

        from holo.discover import DiscoverHandle, SessionStore

        store = SessionStore()
        zc = self._stub_zc()
        with patch(
            "holo.discover._start_browser",
            return_value=(zc, None, store),
        ):
            h = DiscoverHandle(rebrowse_interval_s=0)
            h.start()
            try:
                assert h._rebrowser is None
            finally:
                h.stop()

    def test_swap_browser_preserves_store(self) -> None:
        from unittest.mock import patch

        from holo.discover import DiscoverHandle, SessionStore

        zc1 = self._stub_zc()
        zc2 = self._stub_zc()
        zcs = [zc1, zc2]
        passed_stores: list[Any] = []

        def fake_start(store=None):  # noqa: ANN001
            passed_stores.append(store)
            # First call: handle.start() — no store yet, allocate one.
            # Subsequent call: handle._swap_browser() — reuse passed store.
            actual_store = store if store is not None else SessionStore()
            return (zcs.pop(0), None, actual_store)

        with patch(
            "holo.discover._start_browser", side_effect=fake_start
        ):
            h = DiscoverHandle(rebrowse_interval_s=0)  # disable thread
            h.start()
            initial_store = h._store
            h._swap_browser()

            # Swap passed the existing store to _start_browser, kept it
            # on the handle, and closed the old zc.
            assert passed_stores[0] is None  # initial start
            assert passed_stores[1] is initial_store  # swap reuses
            assert h._store is initial_store
            assert h._zc is zc2
            assert zc1.close_called is True
            h.stop()
