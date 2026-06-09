"""Tai dispatch broker — `/control' WS + `/dispatch' POST endpoints.

Sits between:
  - The tai shell (`@ {sel} prompt /done'), which POSTs JSON to
    /dispatch (see docs/dispatch-protocol.md in the tai repo).
  - The browser droid (xterm in the SPA), which holds a long-lived
    /control WebSocket and accepts `dispatch' messages.

This module owns:
  - DroidRegistry: agent_instance → connected droid websocket.
  - BlockRegistry: block_id → pinned agent_instance (with TTL).
  - PendingDispatches: dispatch_id → asyncio.Future awaited by the
    POST handler.
  - parse_selector + match_against_session: a minimal v1 selector
    parser that handles `*', bare tags, and `name=value' equality
    joined by `,' (AND). Wait config and the full predicate
    grammar are deferred — keep the shell-side selector verbatim
    in the AST and grow this parser as features land.

v1 limitations (documented as TODO in the relevant functions):
  - No predicate `or', no negation, no comparison ops.
  - No wait_config — `timeout_ms' from the request body is the only
    timeout source for now.
  - Broadcast (`[*]') is not yet split out from single (`{*}'); both
    pick exactly one agent. The spec calls for fan-out semantics
    that are easy to add once single is solid.
  - Block stickiness pins by agent_instance only; if the pinned
    droid disconnects, the next dispatch in the block fails fast
    (matches the language spec).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

LOG = logging.getLogger("holo.dispatch")

# Block-id entries idle for this long are evicted. Crash recovery for
# shells that die between `do' and `end'.
BLOCK_TTL_S = 5 * 60


# ---------------------------------------------------------------------------
# selector parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Predicate:
    """One AND-clause of the selector.

    kind="any"        ⇒ matches any agent
    kind="tag"        ⇒ key carries the tag name
    kind="eq"         ⇒ key+value (string equality on TXT field)
    """

    kind: str
    key: str = ""
    value: str = ""


@dataclass(frozen=True)
class Selector:
    raw: str
    broadcast: bool
    predicates: tuple[Predicate, ...]


def parse_selector(raw: str | None) -> Selector | None:
    """Parse a selector clause as sent by the shell.

    Returns None when raw is empty / None (caller falls back to
    $AI_AGENT — but the shell should already have substituted that
    before sending). Returns a Selector otherwise.

    Raises ValueError on a malformed selector. The HTTP handler
    converts that into selector_invalid.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if len(s) < 2 or s[0] not in "{[" or s[-1] not in "}]":
        raise ValueError(f"selector must be wrapped in {{}} or []: {raw!r}")
    open_, close_ = s[0], s[-1]
    if (open_ == "{" and close_ != "}") or (open_ == "[" and close_ != "]"):
        raise ValueError(f"mismatched brackets: {raw!r}")
    broadcast = open_ == "["
    inner = s[1:-1].strip()

    # v1: chop off any wait-config tail (everything after `:'). The
    # actual wait config lives in timeout_ms in the request body for
    # now; once we honour wait_config we'll parse the right side.
    if ":" in inner:
        inner = inner.split(":", 1)[0].strip()

    if inner == "" or inner == "*":
        return Selector(raw=raw, broadcast=broadcast,
                        predicates=(Predicate(kind="any"),))

    preds: list[Predicate] = []
    # AND-joined clauses
    for clause in (c.strip() for c in inner.split(",")):
        if not clause:
            continue
        if "=" in clause:
            k, _, v = clause.partition("=")
            preds.append(Predicate(kind="eq", key=k.strip(), value=v.strip()))
        else:
            # bare tag — match against any TXT field that contains
            # the value, OR a session/instance/host containing it.
            # v1 simplification; will tighten when capability tags
            # are formalized in the announce schema.
            preds.append(Predicate(kind="tag", key=clause))

    if not preds:
        raise ValueError(f"empty selector: {raw!r}")
    return Selector(raw=raw, broadcast=broadcast, predicates=tuple(preds))


def match_session(session: dict[str, Any], selector: Selector) -> bool:
    """Return True when `session' (announce record) satisfies all
    predicates in `selector'. v1 matcher — see module docstring for
    the simplifications."""
    for p in selector.predicates:
        if p.kind == "any":
            continue
        if p.kind == "eq":
            actual = str(session.get(p.key, ""))
            if actual != p.value:
                return False
            continue
        if p.kind == "tag":
            # Match against any string-typed value in the announce
            # record. This is intentionally loose for v1; gets us
            # `{coding}' working without a formal tags field.
            tag = p.key.lower()
            hits = False
            for v in session.values():
                if isinstance(v, str) and tag in v.lower():
                    hits = True
                    break
            if not hits:
                return False
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# registries
# ---------------------------------------------------------------------------


@dataclass
class DroidEntry:
    websocket: WebSocket
    droid_id: str
    agent_instance: str


class DroidRegistry:
    """agent_instance → connected droid WebSocket. Most recent
    registration wins (a re-opened droid replaces the previous
    entry). Closing the WS evicts the entry."""

    def __init__(self) -> None:
        self._by_instance: dict[str, DroidEntry] = {}

    def register(self, entry: DroidEntry) -> None:
        prev = self._by_instance.get(entry.agent_instance)
        if prev is not None and prev.websocket is not entry.websocket:
            LOG.info("dispatch: replacing droid for %s",
                     entry.agent_instance)
        self._by_instance[entry.agent_instance] = entry

    def deregister(self, websocket: WebSocket) -> None:
        for inst, entry in list(self._by_instance.items()):
            if entry.websocket is websocket:
                del self._by_instance[inst]
                return

    def get(self, agent_instance: str) -> DroidEntry | None:
        return self._by_instance.get(agent_instance)

    def known_instances(self) -> set[str]:
        return set(self._by_instance.keys())


@dataclass
class BlockEntry:
    agent_instance: str
    last_used: float = field(default_factory=time.time)


class BlockRegistry:
    """block_id → pinned agent_instance with idle TTL."""

    def __init__(self, ttl_s: float = BLOCK_TTL_S) -> None:
        self._table: dict[str, BlockEntry] = {}
        self._ttl_s = ttl_s

    def get(self, block_id: str) -> str | None:
        self._evict_expired()
        e = self._table.get(block_id)
        if e is None:
            return None
        e.last_used = time.time()
        return e.agent_instance

    def pin(self, block_id: str, agent_instance: str) -> None:
        self._table[block_id] = BlockEntry(agent_instance=agent_instance)

    def release(self, block_id: str) -> None:
        self._table.pop(block_id, None)

    def _evict_expired(self) -> None:
        now = time.time()
        for k, e in list(self._table.items()):
            # `>=' so a `ttl_s=0' (or near-zero) registry behaves
            # as "always evict on access" deterministically across
            # platforms with coarse-grained time.time().
            if now - e.last_used >= self._ttl_s:
                del self._table[k]


# ---------------------------------------------------------------------------
# pending dispatch correlator
# ---------------------------------------------------------------------------


class PendingDispatches:
    """dispatch_id → Future awaited by the /dispatch POST handler.
    The /control WS handler sets the future when a dispatch_result
    arrives carrying that id."""

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def new_future(self, dispatch_id: str, loop: asyncio.AbstractEventLoop
                   ) -> asyncio.Future[dict[str, Any]]:
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._futures[dispatch_id] = fut
        return fut

    def deliver(self, dispatch_id: str, result: dict[str, Any]) -> None:
        fut = self._futures.pop(dispatch_id, None)
        if fut is not None and not fut.done():
            fut.set_result(result)

    def fail_all_for_droid(self, droid_id: str) -> None:
        # We don't track droid_id on each pending; callers fail by
        # passing in a dispatch_id directly. Hook is kept for future
        # use when a droid drops mid-dispatch.
        del droid_id


# ---------------------------------------------------------------------------
# state container — installed in app state via lifespan
# ---------------------------------------------------------------------------


@dataclass
class DispatchState:
    droids: DroidRegistry = field(default_factory=DroidRegistry)
    blocks: BlockRegistry = field(default_factory=BlockRegistry)
    pending: PendingDispatches = field(default_factory=PendingDispatches)


# ---------------------------------------------------------------------------
# /control WS handler
# ---------------------------------------------------------------------------


def make_control_ws(state: DispatchState, sessions_snapshot):
    """Return an async handler for the /control WS endpoint.

    `sessions_snapshot()' returns the current list of announce
    records — used to validate that an agent_instance actually
    exists before accepting a registration."""

    async def control_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        registered: DroidEntry | None = None
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = _json_loads(raw)
                except ValueError:
                    continue
                op = msg.get("op")
                if op == "register":
                    inst = str(msg.get("agent_instance") or "")
                    droid_id = str(msg.get("droid_id") or "")
                    if not inst or not droid_id:
                        await websocket.send_json({
                            "v": 1, "op": "register_ack", "ok": False,
                            "error": "missing_fields",
                        })
                        continue
                    known = {s.get("instance") for s in sessions_snapshot()}
                    if inst not in known:
                        await websocket.send_json({
                            "v": 1, "op": "register_ack", "ok": False,
                            "error": "unknown_instance",
                            "agent_instance": inst,
                        })
                        # We stay connected — diagnostic, the SPA may
                        # still want to log the mismatch.
                        continue
                    registered = DroidEntry(
                        websocket=websocket,
                        droid_id=droid_id,
                        agent_instance=inst,
                    )
                    state.droids.register(registered)
                    await websocket.send_json({
                        "v": 1, "op": "register_ack", "ok": True,
                        "agent_instance": inst,
                    })
                    LOG.info("dispatch: droid %s registered for %s",
                             droid_id, inst)
                elif op == "ping":
                    await websocket.send_json({"v": 1, "op": "pong"})
                elif op == "pong":
                    pass
                elif op == "dispatch_result":
                    did = str(msg.get("dispatch_id") or "")
                    if did:
                        state.pending.deliver(did, msg)
                else:
                    LOG.debug("dispatch: ignoring unknown op %r", op)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - diagnostic
            LOG.warning("dispatch: control WS error: %r", exc)
        finally:
            if registered is not None:
                state.droids.deregister(websocket)
                LOG.info("dispatch: droid for %s disconnected",
                         registered.agent_instance)
            try:
                await websocket.close()
            except Exception:
                pass

    return control_ws


# ---------------------------------------------------------------------------
# /dispatch POST handler
# ---------------------------------------------------------------------------


DEFAULT_TIMEOUT_MS = 30_000


def make_dispatch_endpoint(state: DispatchState, sessions_snapshot):
    """Return an async handler for POST /dispatch."""

    async def dispatch_endpoint(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"v": 1, "ok": False, "error": "selector_invalid"},
                status_code=400,
            )

        # Parse the selector. Empty selector field means the shell
        # didn't pass one; in that case there's no matching to do
        # and we fail with no_match (the language spec calls for
        # $AI_AGENT fall-back at the shell level, not here).
        try:
            sel = parse_selector(body.get("selector"))
        except ValueError as e:
            return JSONResponse(
                {"v": 1, "ok": False, "error": "selector_invalid",
                 "detail": str(e)},
                status_code=400,
            )
        if sel is None:
            return JSONResponse(
                {"v": 1, "ok": False, "error": "no_selector"},
                status_code=400,
            )

        block_id = body.get("block_id")
        timeout_ms = body.get("timeout_ms")
        if not isinstance(timeout_ms, (int, float)) or timeout_ms <= 0:
            timeout_ms = DEFAULT_TIMEOUT_MS

        # Block-pinned routing skips selector matching.
        agent_instance: str | None = None
        if isinstance(block_id, str) and block_id:
            agent_instance = state.blocks.get(block_id)

        if agent_instance is None:
            # Match selector against known sessions.
            sessions = sessions_snapshot()
            matches = [s for s in sessions if match_session(s, sel)]
            if not matches:
                return JSONResponse(
                    {"v": 1, "ok": False, "error": "no_match"},
                )
            # Prefer matches that have a registered droid.
            with_droid = [
                s for s in matches
                if state.droids.get(s.get("instance", "")) is not None
            ]
            if not with_droid:
                return JSONResponse(
                    {"v": 1, "ok": False, "error": "no_droid_attached",
                     "agent_instance": matches[0].get("instance")},
                )
            agent_instance = with_droid[0].get("instance") or ""
            if isinstance(block_id, str) and block_id:
                state.blocks.pin(block_id, agent_instance)

        droid = state.droids.get(agent_instance)
        if droid is None:
            return JSONResponse(
                {"v": 1, "ok": False, "error": "no_droid_attached",
                 "agent_instance": agent_instance},
            )

        # Compose the dispatch payload + sentinel watch.
        dispatch_id = uuid.uuid4().hex[:16]
        nonce = uuid.uuid4().hex[:8]
        prompt = str(body.get("prompt") or "")
        sentinel_id = body.get("sentinel")
        capture = bool(body.get("capture"))

        if isinstance(sentinel_id, str) and sentinel_id:
            expect = f"/{sentinel_id}:{nonce}"
            payload = (
                f"{prompt}\n"
                f"When complete, print exactly:\n"
                f"    {expect}\n"
                f"on its own line.\n"
            )
        else:
            expect = None
            payload = prompt + ("\n" if prompt and not prompt.endswith("\n") else "")

        loop = asyncio.get_running_loop()
        fut = state.pending.new_future(dispatch_id, loop)
        try:
            await droid.websocket.send_json({
                "v": 1, "op": "dispatch",
                "dispatch_id": dispatch_id,
                "payload": payload,
                "expect": expect,
                "timeout_ms": int(timeout_ms),
                "capture": capture,
            })
        except Exception:
            state.droids.deregister(droid.websocket)
            return JSONResponse(
                {"v": 1, "ok": False, "error": "droid_dropped",
                 "agent_instance": agent_instance},
            )

        # Wait for dispatch_result, with a wall-clock guard slightly
        # longer than timeout_ms so the droid's own timer fires first.
        try:
            result = await asyncio.wait_for(
                fut, timeout=(timeout_ms / 1000.0) + 5.0
            )
        except TimeoutError:
            return JSONResponse(
                {"v": 1, "ok": False, "error": "timeout",
                 "agent_instance": agent_instance,
                 "elapsed_ms": int(timeout_ms)},
            )

        return JSONResponse({
            "v": 1,
            "ok": bool(result.get("ok")),
            "agent_instance": agent_instance,
            "nonce": nonce,
            "elapsed_ms": int(result.get("elapsed_ms", 0)),
            **({"error": result["error"]} if not result.get("ok") and result.get("error") else {}),
            **({"output": result["output"]} if capture and result.get("output") else {}),
        })

    return dispatch_endpoint


def make_release_endpoint(state: DispatchState):
    """Return an async handler for POST /dispatch/release."""

    async def release_endpoint(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"v": 1, "ok": False}, status_code=400)
        block_id = body.get("block_id")
        if isinstance(block_id, str) and block_id:
            state.blocks.release(block_id)
        return JSONResponse({"v": 1, "ok": True})

    return release_endpoint


# ---------------------------------------------------------------------------


def _json_loads(s: str) -> dict[str, Any]:
    """Strict-ish JSON load that raises ValueError on non-dict."""
    import json
    obj = json.loads(s)
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    return obj
