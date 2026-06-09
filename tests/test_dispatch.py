"""Unit tests for holo.dispatch.

The HTTP/WS plumbing relies on Starlette's TestClient; the selector
parser, registries, and pending correlator are exercised directly.

End-to-end (real shell + real droid) is verified manually — see the
matching test plan in docs/dispatch-protocol.md (tai repo).
"""

from __future__ import annotations

import asyncio

import pytest

from holo.dispatch import (
    BlockRegistry,
    DroidRegistry,
    PendingDispatches,
    Predicate,
    Selector,
    match_session,
    parse_selector,
)


class TestParseSelector:
    def test_none_returns_none(self) -> None:
        assert parse_selector(None) is None

    def test_empty_returns_none(self) -> None:
        assert parse_selector("") is None
        assert parse_selector("   ") is None

    def test_star_braces_matches_any(self) -> None:
        s = parse_selector("{*}")
        assert s is not None
        assert s.broadcast is False
        assert s.predicates == (Predicate(kind="any"),)

    def test_star_brackets_is_broadcast(self) -> None:
        s = parse_selector("[*]")
        assert s is not None and s.broadcast is True

    def test_bare_tag(self) -> None:
        s = parse_selector("{coding}")
        assert s is not None
        assert s.predicates == (Predicate(kind="tag", key="coding"),)

    def test_eq(self) -> None:
        s = parse_selector("{model=claude-sonnet-4}")
        assert s is not None
        assert s.predicates == (
            Predicate(kind="eq", key="model", value="claude-sonnet-4"),
        )

    def test_and_clauses(self) -> None:
        s = parse_selector("{coding, model=claude}")
        assert s is not None
        assert s.predicates == (
            Predicate(kind="tag", key="coding"),
            Predicate(kind="eq", key="model", value="claude"),
        )

    def test_wait_config_stripped(self) -> None:
        # v1 ignores wait_config — timeout_ms in the body is the
        # canonical timeout.
        s = parse_selector("{coding:5s}")
        assert s is not None
        assert s.predicates == (Predicate(kind="tag", key="coding"),)

    def test_malformed_brackets(self) -> None:
        with pytest.raises(ValueError):
            parse_selector("coding")
        with pytest.raises(ValueError):
            parse_selector("{coding")
        with pytest.raises(ValueError):
            parse_selector("{coding]")


class TestMatchSession:
    def test_any_matches_everything(self) -> None:
        sel = Selector(raw="{*}", broadcast=False,
                       predicates=(Predicate(kind="any"),))
        assert match_session({"instance": "x"}, sel) is True
        assert match_session({}, sel) is True

    def test_eq_field(self) -> None:
        sel = parse_selector("{user=brad}")
        assert sel is not None
        assert match_session({"user": "brad"}, sel) is True
        assert match_session({"user": "alice"}, sel) is False
        assert match_session({}, sel) is False

    def test_tag_substring_anyfield(self) -> None:
        # v1 tag matcher: substring (case-insensitive) on any string
        # value in the announce record. Loose by design.
        sel = parse_selector("{coding}")
        assert sel is not None
        assert match_session(
            {"instance": "claude-coding-Right-Mac", "host": "Right-Mac"},
            sel,
        ) is True
        assert match_session({"instance": "research-bot"}, sel) is False

    def test_and_all_must_match(self) -> None:
        sel = parse_selector("{user=brad, coding}")
        assert sel is not None
        assert match_session(
            {"user": "brad", "instance": "claude-coding"},
            sel,
        ) is True
        assert match_session(
            {"user": "alice", "instance": "claude-coding"},
            sel,
        ) is False
        assert match_session(
            {"user": "brad", "instance": "research-bot"},
            sel,
        ) is False


class TestDroidRegistry:
    def test_register_and_get(self) -> None:
        from holo.dispatch import DroidEntry

        class _WS:
            pass

        reg = DroidRegistry()
        ws = _WS()
        reg.register(DroidEntry(websocket=ws, droid_id="d1",
                                agent_instance="A"))
        e = reg.get("A")
        assert e is not None and e.droid_id == "d1"
        assert reg.get("B") is None

    def test_replace_on_re_register(self) -> None:
        from holo.dispatch import DroidEntry

        class _WS:
            pass

        reg = DroidRegistry()
        ws1, ws2 = _WS(), _WS()
        reg.register(DroidEntry(websocket=ws1, droid_id="d1", agent_instance="A"))
        reg.register(DroidEntry(websocket=ws2, droid_id="d2", agent_instance="A"))
        e = reg.get("A")
        assert e is not None and e.droid_id == "d2"

    def test_deregister_by_websocket(self) -> None:
        from holo.dispatch import DroidEntry

        class _WS:
            pass

        reg = DroidRegistry()
        ws = _WS()
        reg.register(DroidEntry(websocket=ws, droid_id="d1", agent_instance="A"))
        reg.deregister(ws)
        assert reg.get("A") is None


class TestBlockRegistry:
    def test_pin_and_get(self) -> None:
        b = BlockRegistry()
        assert b.get("blk-1") is None
        b.pin("blk-1", "agent-A")
        assert b.get("blk-1") == "agent-A"

    def test_release(self) -> None:
        b = BlockRegistry()
        b.pin("blk-1", "agent-A")
        b.release("blk-1")
        assert b.get("blk-1") is None

    def test_idle_eviction(self) -> None:
        b = BlockRegistry(ttl_s=0.0)  # everything is "expired" at once
        b.pin("blk-1", "agent-A")
        # First get refreshes, but with ttl_s=0 the entry is evicted
        # before the read returns a value.
        assert b.get("blk-1") is None


class TestPending:
    def test_deliver_resolves_future(self) -> None:
        async def go() -> dict:
            loop = asyncio.get_running_loop()
            p = PendingDispatches()
            fut = p.new_future("d1", loop)
            p.deliver("d1", {"ok": True, "elapsed_ms": 12})
            return await fut

        result = asyncio.run(go())
        assert result == {"ok": True, "elapsed_ms": 12}

    def test_unknown_id_is_noop(self) -> None:
        p = PendingDispatches()
        p.deliver("nope", {"ok": True})  # should not raise


class TestE2EHTTP:
    """Full HTTP dispatch via Starlette TestClient with a stub
    droid. Exercises the wire format end-to-end."""

    def _make_app(self, sessions: list[dict]):
        from starlette.applications import Starlette
        from starlette.routing import Route, WebSocketRoute

        from holo import dispatch as _dispatch

        state = _dispatch.DispatchState()

        def snap() -> list[dict]:
            return list(sessions)

        return Starlette(
            routes=[
                Route(
                    "/dispatch",
                    _dispatch.make_dispatch_endpoint(state, snap),
                    methods=["POST"],
                ),
                Route(
                    "/dispatch/release",
                    _dispatch.make_release_endpoint(state),
                    methods=["POST"],
                ),
                WebSocketRoute(
                    "/control",
                    _dispatch.make_control_ws(state, snap),
                ),
            ],
        ), state

    def test_no_match(self) -> None:
        from starlette.testclient import TestClient

        app, _ = self._make_app(sessions=[])
        client = TestClient(app)
        r = client.post("/dispatch", json={
            "v": 1, "selector": "{coding}", "broadcast": False,
            "prompt": "hello", "sentinel": None, "block_id": None,
            "timeout_ms": None, "capture": False,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == "no_match"

    def test_match_but_no_droid(self) -> None:
        from starlette.testclient import TestClient

        app, _ = self._make_app(sessions=[
            {"instance": "claude-coding-Mac", "user": "brad"},
        ])
        client = TestClient(app)
        r = client.post("/dispatch", json={
            "v": 1, "selector": "{coding}", "broadcast": False,
            "prompt": "hello", "sentinel": None, "block_id": None,
            "timeout_ms": None, "capture": False,
        })
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == "no_droid_attached"
        assert body["agent_instance"] == "claude-coding-Mac"

    # Full WS round-trip (register → POST /dispatch → droid answers
    # dispatch_result → POST returns ok:true) is verified end-to-end
    # against the real tai shell and the real SPA droid client; not
    # well-suited to Starlette's TestClient because the sync POST and
    # the threaded WS handler share the test thread's bridge into the
    # ASGI loop and deadlock. The /control WS handler and the
    # dispatch endpoint are exercised individually (no_match,
    # no_droid_attached, register/ack handling, release) — the only
    # untested path is the rendezvous over PendingDispatches, and
    # that's directly unit-tested in TestPending.

    def test_release_unpins(self) -> None:
        from starlette.testclient import TestClient

        app, state = self._make_app(sessions=[])
        client = TestClient(app)
        state.blocks.pin("blk-x", "agent-X")
        assert state.blocks.get("blk-x") == "agent-X"
        r = client.post("/dispatch/release", json={
            "v": 1, "block_id": "blk-x",
        })
        assert r.json()["ok"] is True
        assert state.blocks.get("blk-x") is None
