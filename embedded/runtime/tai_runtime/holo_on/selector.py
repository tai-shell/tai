"""Parser for the ``[holo:...]`` / ``{holo:...}`` selector grammar.

Grammar (informal)::

    selector  := broadcast | single
    broadcast := '[' core ']'
    single    := '{' core '}'
    core      := 'holo:' parts
    parts     := part (':' part)*
    part      := predicate | timeout | settle | mode

    predicate := pred_pair (',' pred_pair)*
    pred_pair := pred_key '=' value
    pred_key  := 'tag' | 'name' | 'host' | 'resource'

    timeout   := DIGIT+ ('s' | 'm' | 'h')
    settle    := 'settle=' DIGIT+ ('ms' | 's')
    mode      := 'tagged' | 'json'

Examples that parse::

    [holo:tag=video-files]
    [holo:tag=video-files:5m]
    [holo:tag=video-files,name=movies:5m:tagged]
    {holo:host=nas-01:30s:json}
    [holo:5m]                              # no predicate, broadcast all
    [holo:tag=a:5m:settle=500ms:tagged]

The order of suffix parts (timeout, settle, mode) is free; the
predicate group must appear FIRST after ``holo:`` because the
predicate split on commas is ambiguous if interleaved with suffix
items. Multiple predicate parts (predicate-then-comma-then-more) are
allowed in a single part — the parser splits each part on '=' to
distinguish predicates from suffix items.

Phase 3.A intentionally parses the FULL grammar even though the
dispatch runtime only acts on a subset (broadcast + default mode). The
3.D mode rollout is then purely runtime work — no parser changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PRED_KEYS: frozenset[str] = frozenset({"tag", "name", "host", "resource"})
_MODES: frozenset[str] = frozenset({"tagged", "json"})

_TIMEOUT_RE = re.compile(r"^(\d+)([smh])$")
_SETTLE_RE = re.compile(r"^settle=(\d+)(ms|s)$")


class SelectorError(ValueError):
    """Raised when a selector string fails to parse.

    Message describes the specific rule that failed so the caller can
    surface it to the user verbatim.
    """


@dataclass(frozen=True)
class Selector:
    """A parsed ``[holo:...]`` / ``{holo:...}`` selector.

    Predicates are normalised to dicts keyed by predicate-name; a
    repeat (``tag=a,tag=b``) raises during parsing rather than silently
    keeping the last one.

    ``timeout_s`` is the resolved seconds-timeout from any ``5m`` /
    ``30s`` / ``1h`` suffix; ``settle_ms`` is the ``settle=Xms`` value
    or None (consumer applies default).

    ``mode`` is one of ``"default"`` / ``"tagged"`` / ``"json"``; the
    dispatch runtime decides what each means for output framing.

    ``broadcast`` distinguishes ``[...]`` (True — fan out to all
    matches) from ``{...}`` (False — dispatch to a single match, error
    if zero or multiple).
    """

    broadcast: bool
    predicates: dict[str, str] = field(default_factory=dict)
    timeout_s: float | None = None
    settle_ms: int | None = None
    mode: str = "default"


def parse_selector(spec: str) -> Selector:
    """Parse a selector string into a :class:`Selector`.

    Raises :class:`SelectorError` with a specific message on any
    structural failure (unmatched brackets, unknown predicate key,
    malformed timeout, etc.).
    """
    if not spec:
        raise SelectorError("selector: empty")
    spec = spec.strip()
    if len(spec) < 2:
        raise SelectorError(
            f"selector {spec!r}: too short — expected '[holo:...]' or "
            "'{holo:...}'"
        )

    if spec[0] == "[" and spec[-1] == "]":
        broadcast = True
    elif spec[0] == "{" and spec[-1] == "}":
        broadcast = False
    else:
        raise SelectorError(
            f"selector {spec!r}: must be wrapped in '[...]' (broadcast) "
            "or '{...}' (single target)"
        )

    inner = spec[1:-1].strip()
    if not inner.startswith("holo:"):
        raise SelectorError(
            f"selector {spec!r}: only the 'holo:' namespace is supported "
            f"(got {inner!r})"
        )
    inner = inner[len("holo:"):]

    if not inner:
        # `[holo:]` is "broadcast to everyone, no predicates, defaults
        # everywhere". Valid — same as `[holo:]` would be if a user
        # really wanted to spell it that way. Just return the default.
        return Selector(broadcast=broadcast)

    parts = inner.split(":")
    predicates: dict[str, str] = {}
    timeout_s: float | None = None
    settle_ms: int | None = None
    mode = "default"
    saw_predicate_part = False
    saw_timeout = False
    saw_settle = False
    saw_mode = False

    for idx, part in enumerate(parts):
        part = part.strip()
        if not part:
            raise SelectorError(
                f"selector {spec!r}: empty segment after ':' at position {idx}"
            )

        # Predicates have `=` AND their first '=' key is a predicate key.
        # Suffix items either lack '=' (timeout / mode) or have key
        # 'settle' (settle override).
        if "=" in part:
            first_key = part.split("=", 1)[0].strip()
            if first_key in _PRED_KEYS:
                if saw_predicate_part:
                    raise SelectorError(
                        f"selector {spec!r}: predicates must appear in "
                        "a single ':'-segment at the start (got a "
                        "second predicate segment at position "
                        f"{idx})"
                    )
                if idx != 0:
                    raise SelectorError(
                        f"selector {spec!r}: predicates must precede "
                        "any timeout / settle / mode suffix (predicate "
                        f"segment found at position {idx})"
                    )
                _parse_predicates(part, predicates, spec)
                saw_predicate_part = True
                continue
            if first_key == "settle":
                if saw_settle:
                    raise SelectorError(
                        f"selector {spec!r}: 'settle' specified twice"
                    )
                settle_ms = _parse_settle(part, spec)
                saw_settle = True
                continue
            raise SelectorError(
                f"selector {spec!r}: unknown key {first_key!r} in "
                f"segment {part!r} (predicates: {sorted(_PRED_KEYS)}, "
                "suffix: 'settle')"
            )

        # No '='. Either a timeout or a mode.
        if _TIMEOUT_RE.match(part):
            if saw_timeout:
                raise SelectorError(
                    f"selector {spec!r}: timeout specified twice"
                )
            timeout_s = _parse_timeout(part, spec)
            saw_timeout = True
            continue
        if part in _MODES:
            if saw_mode:
                raise SelectorError(
                    f"selector {spec!r}: mode specified twice"
                )
            mode = part
            saw_mode = True
            continue
        raise SelectorError(
            f"selector {spec!r}: segment {part!r} is not a recognised "
            "timeout / settle / mode (got: 'Xs'/'Xm'/'Xh' / "
            "'settle=Xms' / 'tagged' / 'json')"
        )

    return Selector(
        broadcast=broadcast,
        predicates=predicates,
        timeout_s=timeout_s,
        settle_ms=settle_ms,
        mode=mode,
    )


def _parse_predicates(
    segment: str, predicates: dict[str, str], spec: str
) -> None:
    """Split ``key=value,key=value,...`` into the predicates dict.

    Raises :class:`SelectorError` on a duplicate key, an unknown key,
    or a malformed pair.
    """
    for pair in segment.split(","):
        pair = pair.strip()
        if not pair:
            raise SelectorError(
                f"selector {spec!r}: empty predicate entry in segment "
                f"{segment!r}"
            )
        if "=" not in pair:
            raise SelectorError(
                f"selector {spec!r}: predicate {pair!r} missing '=' "
                "(expected key=value)"
            )
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in _PRED_KEYS:
            raise SelectorError(
                f"selector {spec!r}: unknown predicate key {key!r} "
                f"(allowed: {sorted(_PRED_KEYS)})"
            )
        if not value:
            raise SelectorError(
                f"selector {spec!r}: predicate {key!r}= has empty value"
            )
        if key in predicates:
            raise SelectorError(
                f"selector {spec!r}: predicate {key!r} specified twice"
            )
        predicates[key] = value


def _parse_timeout(part: str, spec: str) -> float:
    """Convert a ``5m`` / ``30s`` / ``1h`` timeout into seconds."""
    m = _TIMEOUT_RE.match(part)
    if not m:
        # _parse_timeout is only called after _TIMEOUT_RE matched, so
        # this branch is defensive — pre-check should have caught it.
        raise SelectorError(f"selector {spec!r}: bad timeout {part!r}")
    n = int(m.group(1))
    unit = m.group(2)
    if n <= 0:
        raise SelectorError(
            f"selector {spec!r}: timeout {part!r} must be > 0"
        )
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return float(n * multiplier)


def _parse_settle(part: str, spec: str) -> int:
    """Convert ``settle=500ms`` / ``settle=2s`` into milliseconds."""
    m = _SETTLE_RE.match(part)
    if not m:
        raise SelectorError(
            f"selector {spec!r}: bad settle {part!r} (expected "
            "'settle=Xms' or 'settle=Xs')"
        )
    n = int(m.group(1))
    unit = m.group(2)
    if n < 0:
        raise SelectorError(
            f"selector {spec!r}: settle {part!r} must be >= 0"
        )
    return n if unit == "ms" else n * 1000


__all__ = ["Selector", "SelectorError", "parse_selector"]
