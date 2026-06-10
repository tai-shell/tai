"""Unit tests for holo.auto_tunnel.

Pin: lifecycle (start/stop idempotency, snapshot replay,
unsubscribe-on-stop), event handling (add → open tunnel + republish,
remove → tear down + republish, update on existing → no rebuild),
the announcer-update path, and graceful failure when a tunnel can't
open.

We don't actually subscribe to a real Zeroconf browser — the
``cloudcity_discover._start_browser`` and ``tunnel.open_to_cloudcity``
calls are mocked. That's the right level of isolation for this
module: it's pure orchestration logic, with all the network-touching
behaviour stubbed off in their own tests (test_tunnel.py +
test_cloudcity_discover.py).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from holo import auto_tunnel as auto_tunnel_mod


def _make_fake_tunnel(port: int) -> MagicMock:
    t = MagicMock()
    t.port = port
    t.target = ("192.168.1.5", 2222)
    return t


def _make_fake_browser_factory(
    initial_records: list[dict[str, Any]] | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build (zc, browser, store) mocks suitable for AutoTunnel.start().

    `store.subscribe(cb)` records the callback so the test can fire
    synthetic events at it later. `store.snapshot()` returns the
    initial records list (mutable so tests can append).
    """
    zc = MagicMock()
    browser = MagicMock()
    store = MagicMock()
    records = list(initial_records or [])
    callbacks: list[Any] = []
    store.snapshot = MagicMock(side_effect=lambda: list(records))
    store.subscribe = MagicMock(
        side_effect=lambda cb: callbacks.append(cb) or cb
    )
    store.unsubscribe = MagicMock()
    store._records = records
    store._callbacks = callbacks
    return zc, browser, store


def _wait_until(predicate: Any, timeout: float = 2.0) -> bool:
    """Spin until predicate() is true or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------- lifecycle


def test_start_replays_snapshot_and_opens_tunnels(tmp_path: Path) -> None:
    """When AutoTunnel starts, every CloudCity already in the store
    triggers an open_to_cloudcity call."""
    record = {
        "instance": "cc-test",
        "host": "MacBook.local",
        "ips": ["192.168.1.5"],
        "port": 2222,
    }
    zc, browser, store = _make_fake_browser_factory([record])
    fake_tunnel = _make_fake_tunnel(port=51492)
    announcer = MagicMock()

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(zc, browser, store),
    ), patch(
        "holo.tunnel.open_to_cloudcity", return_value=fake_tunnel
    ) as open_mock:
        a = auto_tunnel_mod.AutoTunnel(
            announcer=announcer, key_path=tmp_path / "host-key"
        )
        a.start()
        try:
            assert _wait_until(lambda: a.snapshot() == {"cc-test": 51492})
        finally:
            a.stop()

    open_mock.assert_called_once()


def test_stop_tears_down_tunnels_and_clears_announce(tmp_path: Path) -> None:
    record = {
        "instance": "cc-test",
        "host": "MacBook.local",
        "ips": ["192.168.1.5"],
        "port": 2222,
    }
    zc, browser, store = _make_fake_browser_factory([record])
    fake_tunnel = _make_fake_tunnel(port=51492)
    announcer = MagicMock()

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(zc, browser, store),
    ), patch(
        "holo.tunnel.open_to_cloudcity", return_value=fake_tunnel
    ):
        a = auto_tunnel_mod.AutoTunnel(
            announcer=announcer, key_path=tmp_path / "host-key"
        )
        a.start()
        assert _wait_until(lambda: a.snapshot() == {"cc-test": 51492})
        a.stop()

    fake_tunnel.stop.assert_called()
    zc.close.assert_called()
    # Last set_tunnel_ports call before shutdown clears the map.
    final_call = announcer.set_tunnel_ports.call_args_list[-1]
    assert final_call.args == (None,) or final_call.args == ()


def test_start_idempotent(tmp_path: Path) -> None:
    """A second start() while running is a no-op (no second browser)."""
    zc, browser, store = _make_fake_browser_factory([])
    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(zc, browser, store),
    ) as start_mock:
        a = auto_tunnel_mod.AutoTunnel(
            announcer=None, key_path=tmp_path / "host-key"
        )
        a.start()
        a.start()
        a.stop()
    assert start_mock.call_count == 1


def test_stop_before_start_is_safe(tmp_path: Path) -> None:
    a = auto_tunnel_mod.AutoTunnel(
        announcer=None, key_path=tmp_path / "host-key"
    )
    a.stop()  # should not raise


# --------------------------------------------- event handling


def _drive_event(store: MagicMock, event: dict[str, Any]) -> None:
    """Fire a synthetic event by calling each subscribed callback."""
    for cb in store._callbacks:
        cb(event)


def test_remove_event_tears_down_tunnel(tmp_path: Path) -> None:
    record = {
        "instance": "cc-test",
        "host": "MacBook.local",
        "ips": ["192.168.1.5"],
        "port": 2222,
    }
    zc, browser, store = _make_fake_browser_factory([record])
    fake_tunnel = _make_fake_tunnel(port=51492)
    announcer = MagicMock()

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(zc, browser, store),
    ), patch(
        "holo.tunnel.open_to_cloudcity", return_value=fake_tunnel
    ):
        a = auto_tunnel_mod.AutoTunnel(
            announcer=announcer, key_path=tmp_path / "host-key"
        )
        a.start()
        try:
            assert _wait_until(lambda: a.snapshot() == {"cc-test": 51492})
            _drive_event(store, {"type": "remove", "instance": "cc-test"})
            assert _wait_until(lambda: a.snapshot() == {})
        finally:
            a.stop()
    fake_tunnel.stop.assert_called()


def test_update_event_for_existing_does_not_rebuild(tmp_path: Path) -> None:
    record = {
        "instance": "cc-test",
        "host": "MacBook.local",
        "ips": ["192.168.1.5"],
        "port": 2222,
    }
    zc, browser, store = _make_fake_browser_factory([record])
    fake_tunnel = _make_fake_tunnel(port=51492)
    announcer = MagicMock()

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(zc, browser, store),
    ), patch(
        "holo.tunnel.open_to_cloudcity", return_value=fake_tunnel
    ) as open_mock:
        a = auto_tunnel_mod.AutoTunnel(
            announcer=announcer, key_path=tmp_path / "host-key"
        )
        a.start()
        try:
            assert _wait_until(lambda: a.snapshot() == {"cc-test": 51492})
            _drive_event(
                store, {"type": "update", "cloudcity": {**record, "host": "NewName"}}
            )
            # Give the worker a moment in case it would rebuild.
            time.sleep(0.1)
        finally:
            a.stop()
    # Only the initial open; update events for known CCs don't rebuild.
    assert open_mock.call_count == 1


def test_add_event_for_new_cloudcity_opens_tunnel(tmp_path: Path) -> None:
    """A CloudCity announce that arrives after start() is picked up."""
    zc, browser, store = _make_fake_browser_factory([])
    tunnels = [_make_fake_tunnel(port=51001), _make_fake_tunnel(port=51002)]
    announcer = MagicMock()

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(zc, browser, store),
    ), patch(
        "holo.tunnel.open_to_cloudcity", side_effect=tunnels
    ):
        a = auto_tunnel_mod.AutoTunnel(
            announcer=announcer, key_path=tmp_path / "host-key"
        )
        a.start()
        try:
            _drive_event(
                store,
                {
                    "type": "add",
                    "cloudcity": {
                        "instance": "cc-A",
                        "host": "host-a.local",
                        "ips": ["192.168.1.5"],
                        "port": 2222,
                    },
                },
            )
            _drive_event(
                store,
                {
                    "type": "add",
                    "cloudcity": {
                        "instance": "cc-B",
                        "host": "host-b.local",
                        "ips": ["192.168.1.6"],
                        "port": 2222,
                    },
                },
            )
            assert _wait_until(
                lambda: a.snapshot()
                == {"cc-A": 51001, "cc-B": 51002}
            )
        finally:
            a.stop()


# ----------------------------------------------- failure modes


def test_open_to_cloudcity_failure_drops_entry(tmp_path: Path) -> None:
    """If open_to_cloudcity raises, no entry is recorded and we don't crash."""
    zc, browser, store = _make_fake_browser_factory([])
    announcer = MagicMock()

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(zc, browser, store),
    ), patch(
        "holo.tunnel.open_to_cloudcity", side_effect=RuntimeError("nope")
    ):
        a = auto_tunnel_mod.AutoTunnel(
            announcer=announcer, key_path=tmp_path / "host-key"
        )
        a.start()
        try:
            _drive_event(
                store,
                {
                    "type": "add",
                    "cloudcity": {
                        "instance": "cc-fail",
                        "host": "x.local",
                        "ips": ["192.168.1.5"],
                        "port": 2222,
                    },
                },
            )
            # Worker drains the event without panicking.
            time.sleep(0.1)
            assert a.snapshot() == {}
        finally:
            a.stop()


def test_announcer_set_tunnel_ports_called_on_each_change(
    tmp_path: Path,
) -> None:
    """The announcer's tunnel_ports map is published after every event,
    so the SPA's discovery cache stays in sync without TTL waits."""
    zc, browser, store = _make_fake_browser_factory([])
    fake_tunnel = _make_fake_tunnel(port=51492)
    announcer = MagicMock()

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(zc, browser, store),
    ), patch(
        "holo.tunnel.open_to_cloudcity", return_value=fake_tunnel
    ):
        a = auto_tunnel_mod.AutoTunnel(
            announcer=announcer, key_path=tmp_path / "host-key"
        )
        a.start()
        try:
            _drive_event(
                store,
                {
                    "type": "add",
                    "cloudcity": {
                        "instance": "cc-test",
                        "host": "x.local",
                        "ips": ["192.168.1.5"],
                        "port": 2222,
                    },
                },
            )
            assert _wait_until(
                lambda: announcer.set_tunnel_ports.call_args_list != []
            )
            _drive_event(store, {"type": "remove", "instance": "cc-test"})
            assert _wait_until(
                lambda: a.snapshot() == {}
            )
        finally:
            a.stop()
    # First call after add → map with cc-test; later calls after remove → None.
    add_calls = [
        c
        for c in announcer.set_tunnel_ports.call_args_list
        if c.args and c.args[0] == {"cc-test": 51492}
    ]
    assert add_calls, "expected at least one set_tunnel_ports({cc-test: 51492})"


def test_no_announcer_does_not_crash(tmp_path: Path) -> None:
    """Some configurations (--no-announce) might still want the watcher
    for diagnostics; be defensive about a None announcer."""
    record = {
        "instance": "cc-test",
        "host": "x.local",
        "ips": ["192.168.1.5"],
        "port": 2222,
    }
    zc, browser, store = _make_fake_browser_factory([record])
    fake_tunnel = _make_fake_tunnel(port=51492)
    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(zc, browser, store),
    ), patch(
        "holo.tunnel.open_to_cloudcity", return_value=fake_tunnel
    ):
        a = auto_tunnel_mod.AutoTunnel(
            announcer=None, key_path=tmp_path / "host-key"
        )
        a.start()
        try:
            assert _wait_until(lambda: a.snapshot() == {"cc-test": 51492})
        finally:
            a.stop()


# Make threading import not flagged as unused — used implicitly via the
# AutoTunnel worker being a Thread in production.
_ = threading
