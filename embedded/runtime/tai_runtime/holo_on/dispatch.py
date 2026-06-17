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
import json
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
) -> dict[str, Any]:
    """End-to-end dispatch. Returns a result dict.

    Return shape::

        {"exit": int, "hosts": [{"host": str, "resource": str, "exit": int}, ...]}

    ``exit`` is ``max(per-host exit_code)`` (or 2 on selector parse
    error / single-target ambiguity, 0 on broadcast-with-no-matches).
    ``hosts`` is the per-target outcome list, in target order — the
    C bridge binds this as the ``$ON_HOSTS`` bash array after the
    dispatch returns. Callers that only care about the exit can do
    ``result["exit"]``.

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
         dispatch the body per (host, resource), concurrently via
         ``asyncio.gather`` (Phase 3.D.2).
      6. Combine per-host exit codes via max() for the top-level
         exit, but the full per-host outcome list is returned so
         the C side can populate ``$ON_HOSTS``.
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
        return {"exit": 2, "hosts": []}

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
) -> dict[str, Any]:
    if not sessions:
        if selector.broadcast:
            print(
                "holo-on: no daemons matched selector — nothing to do",
                file=stderr,
            )
            return {"exit": 0, "hosts": []}
        print(
            "holo-on: single-target selector matched zero daemons",
            file=stderr,
        )
        return {"exit": 2, "hosts": []}
    if not selector.broadcast and len(sessions) > 1:
        print(
            f"holo-on: single-target selector matched "
            f"{len(sessions)} daemons (expected exactly one); "
            f"narrow the predicates",
            file=stderr,
        )
        return {"exit": 2, "hosts": []}

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
        return {"exit": 2 if not selector.broadcast else 0, "hosts": []}

    timeout_s = (
        int(selector.timeout_s) if selector.timeout_s is not None else 60
    )

    # Concurrent fan-out. Each target spawns its own `holo mcp`
    # subprocess and runs its MCP round-trip on the event loop;
    # asyncio.gather waits for all of them. Output framing is
    # emitted per-target as each finishes (in completion order, not
    # target order) — for default mode each frame's `print()` is
    # line-atomic under the GIL, so concurrent emits never split a
    # line mid-string. For ``:tagged`` / ``:json`` the host prefix
    # makes interleave order irrelevant.
    async def _run_one(
        session: dict[str, Any], resource: dict[str, Any]
    ) -> dict[str, Any]:
        host = session.get("host", "?")
        rec: dict[str, Any] = {
            "host": host,
            "resource": resource["name"],
            "exit": 0,
        }
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
            rec["exit"] = 1
            return rec
        if "error" in result:
            print(
                f"holo-on: {host}:{resource['name']}: "
                f"{result['error']}: {result.get('message', '')}",
                file=stderr,
            )
            rec["exit"] = 1
            return rec
        _emit_frames(
            host,
            resource["name"],
            result,
            stdout=stdout,
            stderr=stderr,
            mode=selector.mode,
        )
        rec["exit"] = int(result.get("exit", 0))
        return rec

    host_results = await asyncio.gather(
        *(_run_one(session, resource) for session, resource in targets)
    )
    exits = [r["exit"] for r in host_results]
    return {
        "exit": max(exits) if exits else 0,
        "hosts": host_results,
    }


def dispatch_from_c(selector_str: str, body: str) -> dict[str, Any]:
    """Entry point called from C (``holo_on_dispatch.c``).

    Wraps :func:`dispatch` for the bash-side ``on`` keyword: the C
    bridge has the parsed selector + body strings but no convenient
    way to pass file objects, so we default to ``sys.stdout`` /
    ``sys.stderr`` (which the embedded CPython sets up to point at
    the tai shell's actual stdout/stderr). ``HOLO_CLI`` env var
    overrides the spawned daemon command for testing.

    Returns the same dict :func:`dispatch` returns:
    ``{"exit": int, "hosts": [{"host", "resource", "exit"}, ...]}``.
    The C bridge unpacks ``exit`` as the shell command's ``$?`` and
    binds ``hosts`` as the ``$ON_HOSTS`` bash array (3.D.3).
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
    mode: str = "default",
) -> None:
    """Write the response's frames to the caller's streams per Q4.

    Three modes:

      ``default`` — stdout frames written raw (caller self-tags via
                    ``$HOLO_HOST`` if attribution matters); stderr
                    frames always prefixed with ``host:resource:``.

      ``tagged``  — every line (stdout AND stderr) prefixed with
                    ``host:resource:``. The asymmetry of default is
                    gone; piping through awk needs the column-shift
                    awareness called out in the design doc.

      ``json``    — every frame emitted as one JSONL object on
                    stdout: ``{"host","resource","fd","data"}``.
                    Stderr frames are routed to stdout in this mode
                    so consumers piping the JSONL get the full
                    stream in one place; lossless multiplexing.

    Each frame is written as a single line + trailing newline so
    callers can pipe through ``awk`` / ``sort`` / etc. without
    worrying about partial lines from concurrent targets.
    """
    for frame in result.get("frames", ()):
        fd = frame.get("fd")
        data = frame.get("data", "")
        if mode == "json":
            print(
                json.dumps(
                    {
                        "host": host,
                        "resource": resource_name,
                        "fd": fd,
                        "data": data,
                    }
                ),
                file=stdout,
                flush=True,
            )
        elif mode == "tagged":
            target = stderr if fd == "stderr" else stdout
            print(f"{host}:{resource_name}: {data}", file=target, flush=True)
        else:  # default
            if fd == "stderr":
                print(
                    f"{host}:{resource_name}: {data}", file=stderr, flush=True
                )
            else:
                print(data, file=stdout, flush=True)
