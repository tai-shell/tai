"""Shell-side dispatch for the `holo` builtin.

Called from the C shell builtin in builtins/holo.def. Takes a tool name
and a list of string args, looks up the tool in tai_runtime.server,
coerces args to the tool's declared parameter types, calls it, and
returns a formatted string suitable for the shell's stdout.

Type coercion is intentionally minimal — int / float / bool from string,
plus None for missing optionals. Anything more complex (JSON dict for
`region`, etc.) is forwarded to the tool as a raw string and lets the
tool fail loudly if it can't parse.

Result formatting policy matches what shell users naturally want:
- single-key dict where the value is scalar -> print just the value
- everything else -> compact JSON

The control shell's MCP transport gets full structured returns; the
shell builtin gets ergonomic strings. Same underlying functions.
"""

from __future__ import annotations

import inspect
import json
import sys
import typing
from typing import Any

from tai_runtime import server


def _coerce(value: str, annotation: Any) -> Any:
    """Best-effort string -> annotation conversion. Falls back to the
    original string when the annotation isn't a primitive we recognize."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return value
    # Strip Optional[...] / X | None
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            annotation = args[0]
    if annotation is str:
        return value
    if annotation is int:
        return int(value)
    if annotation is float:
        return float(value)
    if annotation is bool:
        low = value.lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"can't parse {value!r} as bool")
    return value


def _format(result: Any) -> str:
    """Render a tool result for the shell. Scalar -> bare value, dict
    with one scalar entry -> bare value, everything else -> JSON."""
    if result is None:
        return ""
    if isinstance(result, (str, int, float, bool)):
        return str(result)
    if isinstance(result, dict) and len(result) == 1:
        only = next(iter(result.values()))
        if isinstance(only, (str, int, float, bool)):
            return str(only)
    return json.dumps(result, separators=(",", ":"))


def dispatch(tool_name: str, str_args: list[str]) -> int:
    """Run `tool_name(*str_args)` and print the formatted result.

    Returns 0 on success, 1 on tool error, 2 on usage error (unknown
    tool, wrong arity). Diagnostics go to stderr; the formatted
    result (if any) goes to stdout."""
    fn = getattr(server, tool_name, None)
    if fn is None or not callable(fn) or tool_name.startswith("_"):
        print(f"holo: unknown tool {tool_name!r}", file=sys.stderr)
        return 2

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        print(f"holo: cannot introspect {tool_name}", file=sys.stderr)
        return 2

    params = list(sig.parameters.values())
    # No starargs in our tool defs, so simple positional binding works.
    bound: list[Any] = []
    for i, p in enumerate(params):
        if i < len(str_args):
            try:
                bound.append(_coerce(str_args[i], p.annotation))
            except (ValueError, TypeError) as e:
                print(f"holo: bad arg {i + 1} for {tool_name}: {e}",
                      file=sys.stderr)
                return 2
        elif p.default is not inspect.Parameter.empty:
            bound.append(p.default)
        else:
            print(f"holo: {tool_name} requires {p.name}", file=sys.stderr)
            return 2

    if len(str_args) > len(params):
        extra = len(str_args) - len(params)
        print(f"holo: {tool_name} got {extra} extra arg(s)", file=sys.stderr)
        return 2

    try:
        result = fn(*bound)
    except Exception as e:
        print(f"holo: {tool_name}: {e}", file=sys.stderr)
        return 1

    out = _format(result)
    if out:
        print(out)
    return 0
