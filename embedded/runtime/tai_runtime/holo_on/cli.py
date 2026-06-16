"""Argparse + entry function for ``holo-on``.

Phase 3.A scope: validate the end-to-end dispatch by accepting a
selector string + body string from the CLI and exiting with
``max(per-host exit codes)`` (or 2 on usage errors). Output to stdout
matches the design doc Q4 defaults — raw line-atomic concat per host,
stderr always host-tagged.

Usage::

    python -m tai_runtime.holo_on '[holo:tag=video-files:5m]' 'find . -name "*.mp4" | wc -l'

The CLI is the validation harness for Phase 3.A. The bash-level
``on`` keyword (Phase 3.B) calls into the same dispatch code via the
embedded interpreter, not through this argparse layer.
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="holo-on",
        description=(
            "Phase 3.A: dispatch a shell body to every holo daemon "
            "matching a selector. Equivalent to tai's planned `on "
            "SELECTOR BODY` keyword but invoked from a regular shell."
        ),
    )
    p.add_argument(
        "selector",
        help=(
            "Selector grammar: `[holo:PRED:SUFFIX]` (broadcast) or "
            "`{holo:PRED:SUFFIX}` (single target). PRED is comma-joined "
            "(AND): `tag=X`, `name=X`, `host=X`, `resource=X`. SUFFIX "
            "is colon-joined (any order): `5m` (timeout), `settle=500ms`, "
            "`tagged`, `json`."
        ),
    )
    p.add_argument(
        "body",
        help=(
            "Shell body to run on each matching host. cwd is pinned to "
            "the resource path; HOLO_HOST / HOLO_RESOURCE / "
            "HOLO_RESOURCE_PATH are injected into env."
        ),
    )
    p.add_argument(
        "--holo-command",
        default="holo",
        help=(
            "Command used to spawn the local holo daemon for MCP stdio "
            "(Phase 3.A v1 only talks to one local daemon at a time). "
            "Default 'holo' on PATH."
        ),
    )
    p.add_argument(
        "--holo-extra-arg",
        action="append",
        default=[],
        help=(
            "Extra argument passed to the spawned 'holo mcp' subprocess "
            "(repeatable). Use to point at the daemon's resources "
            "config and any other startup flags. Example: "
            "--holo-extra-arg --resources-config "
            "--holo-extra-arg /path/to/resources.toml"
        ),
    )
    return p


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    from tai_runtime.holo_on.dispatch import dispatch

    return dispatch(
        selector_str=args.selector,
        body=args.body,
        holo_command=args.holo_command,
        holo_extra_args=args.holo_extra_arg,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
