"""Tests for ChannelRegistry — the daemon-side sid → Channel map."""

from __future__ import annotations

import threading

from holo.registry import ChannelRegistry


class _Stub:
    def __init__(self, name: str) -> None:
        self.name = name


def test_register_and_lookup():
    reg = ChannelRegistry()
    a = _Stub("a")
    reg.register("sid-1", a)
    assert reg.lookup("sid-1") is a
    assert reg.lookup("missing") is None
    assert len(reg) == 1


def test_unregister_idempotent():
    reg = ChannelRegistry()
    reg.register("sid-1", _Stub("a"))
    reg.unregister("sid-1")
    reg.unregister("sid-1")  # second call must not raise
    assert reg.lookup("sid-1") is None
    assert len(reg) == 0


def test_concurrent_register_lookup_safe():
    reg = ChannelRegistry()
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        try:
            for i in range(2000):
                reg.register(f"sid-{i % 50}", _Stub(str(i)))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    def reader():
        while not stop.is_set():
            for i in range(50):
                reg.lookup(f"sid-{i}")

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
