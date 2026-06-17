"""Top-level dispatch orchestration for ``holo-on`` / ``on`` keyword.

Glues together:

  1. :mod:`tai_runtime.holo_on.selector` — parse the selector string
  2. :mod:`tai_runtime.holo_on.discovery` — find matching mDNS sessions
  3. :mod:`tai_runtime.holo_on.mcp_client` — for each match, call
     ``holo_list_resources`` to enumerate per-host resources, filter
     by the selector's resource-side predicates, then call
     ``holo_exec_in_resource`` per (host, resource) tuple
  4. Output emission — frames written to caller's stdout/stderr per
     Q4 defaults (raw line-atomic concat; stderr always host-tagged)
  5. Exit semantics — combined exit = max(per-host exits); 2 on
     usage / setup errors

Phase 3.A scope simplification: the production transport is ``holo
mcp`` over the auto-tunnel (Q1). For 3.A's CLI validation we instead
spawn ``holo mcp`` locally per dispatched target — same code path on
the daemon side, no tunnel acquisition. The MCP client's
``extra_args`` lets the harness pass the daemon's ``--resources-config``
so the spawned daemon mirrors the announcing one. 3.B+ swaps in the
real transport.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, TextIO

from tai_runtime.holo_on.discovery import find_matches
from tai_runtime.holo_on.mcp_client import (
    HoloMCPClientError,
    exec_in_resource,
    list_resources,
)
from tai_runtime.holo_on.selector import (
    Selector,  # noqa: F401 — kept for type hints in _dispatch_async
    SelectorError,
    parse_selector,
)


def dispatch(
    *,
    selector_str: str,
    body: str,
    holo_command: str = "holo",
    holo_extra_args: list[str] | None = None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """End-to-end dispatch. Returns the process exit code.

    Top-level flow:

      1. Parse ``selector_str``. Bad selector → 2 with the error
         text on stderr.
      2. Discover matching mDNS sessions.
      3. For broadcast ``[holo:...]`` with zero matches: emit a
         warning on stderr, exit 0 (nothing to do, not an error).
      4. For single-target ``{holo:...}`` with zero or >1 matches:
         error on stderr, exit 2 (the selector named one daemon and
         we got the wrong number).
      5. For each match: connect to its MCP endpoint, list its
         resources, filter by the selector's resource-name predicate,
         dispatch the body per (host, resource).
      6. Combine per-host exit codes via max().

    Output:
      - Each frame's ``data`` is written to ``stdout`` as a line for
        stdout frames; to ``stderr`` for stderr frames (always with a
        ``host:`` prefix on stderr per Q4).
      - Order across hosts is determined by per-target completion
        order — Phase 3.A waits for each target sequentially. 3.B+
        will fan out concurrently.
    """
    extra = list(holo_extra_args or [])
    # holo's CLI requires --announce to be present whenever --resources-
    # config or --announce-resource is also set. Phase 3.A's local-spawn
    # pattern (one holo subprocess per dispatch target) means we need
    # --announce on the spawned daemon's args even though we don't care
    # about its mDNS broadcast. Add it implicitly if the caller passed a
    # resources config / explicit resource declaration — saves the user
    # one obvious flag they'd otherwise hit a CLI error on.
    needs_announce = any(
        a in ("--resources-config", "--announce-resource") for a in extra
    )
    if needs_announce and "--announce" not in extra:
        extra = ["--announce"] + extra

    try:
        selector = parse_selector(selector_str)
    except SelectorError as e:
        print(f"holo-on: {e}", file=stderr)
        return 2

    # Run discovery OUTSIDE asyncio.run — python-zeroconf's sync API
    # and asyncio don't compose well in our setup (callbacks delivered
    # by the zeroconf threads stop reaching the SessionStore once an
    # asyncio loop is installed in the main thread). The mcp library
    # requires asyncio, so dispatch runs in two phases: discovery
    # synchronously up here, MCP work async below.
    sessions = find_matches(selector)

    return asyncio.run(
        _dispatch_async(
            selector=selector,
            sessions=sessions,
            body=body,
            holo_command=holo_command,
            extra_args=extra,
            stdout=stdout,
            stderr=stderr,
        )
    )


async def _dispatch_async(
    *,
    selector: Selector,
    sessions: list[dict[str, Any]],
    body: str,
    holo_command: str,
    extra_args: list[str],
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if not sessions:
        if selector.broadcast:
            print(
                "holo-on: no daemons matched selector — nothing to do",
                file=stderr,
            )
            return 0
        print(
            "holo-on: single-target selector matched zero daemons",
            file=stderr,
        )
        return 2
    if not selector.broadcast and len(sessions) > 1:
        print(
            f"holo-on: single-target selector matched "
            f"{len(sessions)} daemons (expected exactly one); "
            f"narrow the predicates",
            file=stderr,
        )
        return 2

    targets = []
    for session in sessions:
        try:
            resources = await list_resources(
                holo_command, extra_args=extra_args
            )
        except HoloMCPClientError as e:
            host = session.get("host", "?")
            print(f"holo-on: {host}: list_resources failed: {e}", file=stderr)
            continue
        wanted_resource = (
            selector.predicates.get("resource")
            or selector.predicates.get("name")
        )
        for r in resources:
            if wanted_resource and r.get("name") != wanted_resource:
                continue
            # When no resource predicate, dispatch to every resource
            # the daemon exposes — matches the design doc Q5 reading
            # of "a host announcing two tagged volumes shows up twice."
            targets.append((session, r))

    if not targets:
        print(
            "holo-on: no resources matched on any discovered daemon",
            file=stderr,
        )
        return 2 if not selector.broadcast else 0

    timeout_s = (
        int(selector.timeout_s) if selector.timeout_s is not None else 60
    )
    exits: list[int] = []
    for session, resource in targets:
        host = session.get("host", "?")
        try:
            result = await exec_in_resource(
                holo_command,
                resource["name"],
                body,
                timeout_s=timeout_s,
                extra_args=extra_args,
            )
        except HoloMCPClientError as e:
            print(f"holo-on: {host}: exec failed: {e}", file=stderr)
            exits.append(1)
            continue
        if "error" in result:
            print(
                f"holo-on: {host}:{resource['name']}: "
                f"{result['error']}: {result.get('message', '')}",
                file=stderr,
            )
            exits.append(1)
            continue
        _emit_frames(
            host, resource["name"], result, stdout=stdout, stderr=stderr
        )
        exits.append(int(result.get("exit", 0)))

    return max(exits) if exits else 0


def dispatch_from_c(selector_str: str, body: str) -> int:
    """Entry point called from C (``holo_on_dispatch.c``).

    Wraps :func:`dispatch` for the bash-side ``on`` keyword: the C
    bridge has the parsed selector + body strings but no convenient
    way to pass file objects, so we default to ``sys.stdout`` /
    ``sys.stderr`` (which the embedded CPython sets up to point at
    the tai shell's actual stdout/stderr). ``HOLO_CLI`` env var
    overrides the spawned daemon command for testing.

    Returns the int exit code the C bridge propagates back to the
    shell as the command's ``$?``.
    """
    holo_cmd = os.environ.get("HOLO_CLI") or "holo"
    extra_env = os.environ.get("HOLO_ON_EXTRA_ARGS")
    extra = extra_env.split(None) if extra_env else None
    return dispatch(
        selector_str=selector_str,
        body=body,
        holo_command=holo_cmd,
        holo_extra_args=extra,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _emit_frames(
    host: str,
    resource_name: str,
    result: dict[str, Any],
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    """Write the response's frames to the caller's streams per Q4.

    Phase 3.A only implements the **default mode**: raw line-atomic
    concat to stdout for stdout frames, ``host:`` prefix on stderr
    for stderr frames. Modes ``:tagged`` and ``:json`` are deferred
    to Phase 3.D.

    Each frame is written as a single line + trailing newline so
    callers can pipe through ``awk`` / ``sort`` / etc. without
    worrying about partial lines from concurrent targets.
    """
    for frame in result.get("frames", ()):
        fd = frame.get("fd")
        data = frame.get("data", "")
        if fd == "stderr":
            print(f"{host}:{resource_name}: {data}", file=stderr, flush=True)
        else:
            print(data, file=stdout, flush=True)
