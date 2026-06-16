"""LAN discovery: zeroconf browse + selector-side filtering.

Phase 3.A uses holo's vendored :class:`holo.discover.DiscoverHandle`
as the underlying browser — it's already maintenance-free (rebrowse
thread, stale sweeper, the works) and exposes a
:class:`SessionStore` we can snapshot per dispatch.

We wrap it with:

  1. Per-process lazy init (the handle starts on first ``find_matches``)
  2. A settle window applied PER dispatch (caller passes a Selector;
     we sleep up to ``settle_ms`` waiting for the browser to populate
     before snapshotting)
  3. Selector-side filtering by ``tag`` / ``name`` / ``host``
     predicates — the cheap mDNS-level filter described in Q5 of the
     resources doc. The ``resource`` predicate (resource NAME match)
     is partly handled here (via TXT's ``rn=`` summary) but final
     resolution happens after the per-daemon MCP
     ``holo_list_resources`` call in :mod:`dispatch`.

Phase 3.A scope simplification: the handle is created here and kept
on a module-level slot. Phase 3.B will move this onto a shell-lifetime
state object so the cache persists across the embedded interpreter's
calls.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from holo.announce import FIELD_HOST, FIELD_R, FIELD_RN

if TYPE_CHECKING:
    from tai_runtime.holo_on.selector import Selector


# Default settle window when the selector doesn't override (:settle=Xms).
# Matches the Phase 0 doc's Q6 decision.
DEFAULT_SETTLE_MS = 1000


_handle_lock = threading.Lock()
_handle: Any | None = None


def _get_or_start_handle() -> Any:
    """Lazy-init the DiscoverHandle on first call.

    Subsequent dispatches reuse the same handle, which keeps the
    zeroconf browser hot (no per-call mDNS warmup cost). Phase 3.B
    will move this to per-shell state; for 3.A's CLI a process-level
    singleton is fine since one process == one dispatch.
    """
    global _handle
    with _handle_lock:
        if _handle is not None:
            return _handle
        from holo.discover import DiscoverHandle

        _handle = DiscoverHandle()
        _handle.start()
        return _handle


def shutdown_handle() -> None:
    """Stop the handle, if any. Test/teardown helper."""
    global _handle
    with _handle_lock:
        h = _handle
        _handle = None
    if h is not None:
        h.stop()


def find_matches(selector: Selector) -> list[dict[str, Any]]:
    """Return the live LAN sessions matching ``selector``'s mDNS-side predicates.

    Applies the settle window (selector override if set, else
    ``DEFAULT_SETTLE_MS``) BEFORE snapshotting — gives the zeroconf
    browser time to populate on a cold cache.

    Predicates applied here:

      - ``tag=X`` → session's ``r`` (FIELD_R) list must contain X
      - ``name=X`` → session's ``rn`` (FIELD_RN) list must contain X
      - ``host=X`` → session's ``host`` field equals X
      - ``resource=X`` → same as ``name=X`` at this layer (resource
        names are in FIELD_RN). The dispatch layer cross-checks
        against the full holo_list_resources result for correctness.

    Returns the matching session dicts in arrival order. Empty list if
    no match — the dispatcher decides what that means for broadcast vs
    single-target.
    """
    handle = _get_or_start_handle()
    settle_ms = (
        selector.settle_ms
        if selector.settle_ms is not None
        else DEFAULT_SETTLE_MS
    )
    if settle_ms > 0:
        time.sleep(settle_ms / 1000.0)

    store = handle.store
    if store is None:
        return []
    snapshot = store.snapshot()
    return [s for s in snapshot if _matches(s, selector)]


def _matches(session: dict[str, Any], selector: Selector) -> bool:
    """True iff the session matches every selector predicate.

    Unknown predicates are NOT silently ignored — the parser already
    rejected anything outside {tag, name, host, resource}, so any
    predicate that reaches here is one of those four. This keeps the
    predicate→field mapping legible at one site.
    """
    for key, value in selector.predicates.items():
        if key == "tag":
            tags = session.get(FIELD_R) or []
            if value not in tags:
                return False
        elif key == "name" or key == "resource":
            names = session.get(FIELD_RN) or []
            if value not in names:
                return False
        elif key == "host":
            if session.get(FIELD_HOST) != value:
                return False
        else:
            # Defensive — the parser shouldn't let this through.
            return False
    return True


__all__ = [
    "DEFAULT_SETTLE_MS",
    "find_matches",
    "shutdown_handle",
]
