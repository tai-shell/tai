"""Shell-side dispatch for the `holo` builtin.

Called from the C shell builtin in builtins/holo.def. Takes a tool name
and a list of string args, looks up the tool in tai_runtime.server,
coerces args to the tool's declared parameter types, calls it, and
returns a formatted string suitable for the shell's stdout.

Type coercion is intentionally minimal — int / float / bool from string,
plus None for missing optionals, JSON-decode for dict/list, and a
varargs-style slurp for a trailing list[T] parameter so the user can
write `screen_click 480 320 cmd shift` instead of
`screen_click 480 320 '["cmd","shift"]'`.

Result formatting policy matches what shell users naturally want:
- None              -> nothing printed (so `[ -z "$out" ]` reliably tests
                       "tool returned nothing", distinct from "" results)
- scalar            -> bare value
- single-key dict   -> bare value (when the value is scalar)
- everything else   -> compact JSON

The control shell's MCP transport gets full structured returns; the
shell builtin gets ergonomic strings. Same underlying functions.

Tool names are validated against the FastMCP tool registry so that
plain module-level callables (server.serve, imported classes like
FastMCP / BridgeClient / TemplateStore) cannot be invoked by accident
through `holo <name>` — `holo serve` would otherwise hijack bash's
stdio as an MCP transport.
"""

from __future__ import annotations

import inspect
import json
import sys
import types
import typing
from typing import Any

# PEP 604 unions (X | None) get_origin to types.UnionType. PEP 484
# unions (Optional[X]) get_origin to typing.Union. Treat them the
# same — both flatten to the underlying X when None is the only
# alternative.
_UNION_ORIGINS = (typing.Union, types.UnionType)

from tai_runtime import server


# ---------------------------------------------------------------------------
# Tool allow-list — populated lazily from the FastMCP registry.
# ---------------------------------------------------------------------------

_TOOL_NAMES: frozenset[str] | None = None


def _valid_tool_names() -> frozenset[str]:
    """Return the set of names registered as FastMCP tools on server._app.

    Lazy + cached so import order can't race with decorator registration.
    Reaches into _tool_manager (a private FastMCP attribute) consistent
    with server.py's own usage; if FastMCP renames it, this fails fast
    on first dispatch rather than silently exposing the wrong surface."""
    global _TOOL_NAMES
    if _TOOL_NAMES is None:
        tools = server._app._tool_manager.list_tools()  # noqa: SLF001
        _TOOL_NAMES = frozenset(t.name for t in tools)
    return _TOOL_NAMES


# ---------------------------------------------------------------------------
# Annotation helpers
# ---------------------------------------------------------------------------


def _strip_optional(annotation: Any) -> tuple[Any, bool]:
    """If annotation is Optional[X] / X | None, return (X, True).
    Otherwise return (annotation, False). Used to handle empty-string
    inputs uniformly: an empty string in a positional slot whose type
    accepts None means 'use the default / None'."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return annotation, False
    origin = typing.get_origin(annotation)
    if origin in _UNION_ORIGINS:
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0], True
    return annotation, False


def _is_list_annotation(annotation: Any) -> bool:
    return annotation is list or typing.get_origin(annotation) is list


# ---------------------------------------------------------------------------
# Argument coercion
# ---------------------------------------------------------------------------


def _coerce(value: str, annotation: Any) -> Any:
    """Best-effort string -> annotation conversion. Falls back to the
    original string when the annotation isn't a primitive we recognize."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return value
    # Strip Optional[...] / X | None
    annotation, was_optional = _strip_optional(annotation)
    # Empty string for an Optional-typed param == None. Lets a shell
    # user write `ui_template_capture mylabel myapp '' true` to skip
    # the region arg and pass the next positional.
    if was_optional and value == "":
        return None
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
    # dict / list / nested structure -> JSON-decode the string. Lets a
    # shell user pass things like `screen_shot '{"x":0,...}'` without
    # giving up the typed Python API. Trailing list[T] params are
    # handled by _coerce_list_slurp in dispatch() before we get here.
    origin = typing.get_origin(annotation)
    if annotation is dict or origin is dict or annotation is list or origin is list:
        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"expected JSON {annotation}, got {value!r}: {e}") from e
    # Fallthrough: anything outside (str, int, float, bool, dict, list) —
    # typing.Literal, typing.TypeAlias, custom classes, etc. — gets passed
    # through as the raw string. Fine for the current 25-tool surface
    # (none use those types). Revisit if a tool grows e.g. a Literal
    # enum param where the caller expects pre-validation here.
    return value


def _coerce_list_slurp(values: list[str], annotation: Any) -> list[Any]:
    """Coerce each string in `values` to the element type of `annotation`
    (a list[T] type). Used when a trailing list[T] parameter slurps the
    remaining positional shell args.

    `screen_click 480 320 cmd shift` -> modifiers=['cmd', 'shift'].
    No JSON involved on this path; the user passes plain shell words."""
    args = typing.get_args(annotation)
    elem_type = args[0] if args else str
    return [_coerce(v, elem_type) for v in values]


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


def _format(result: Any) -> str:
    """Render a non-None tool result for the shell. Scalar -> bare value,
    dict with one scalar entry -> bare value, everything else -> JSON.

    None is handled separately in dispatch (no output, exit 0) so a
    shell caller can distinguish 'tool returned nothing' from 'tool
    returned the empty string'."""
    if isinstance(result, (str, int, float, bool)):
        return str(result)
    if isinstance(result, dict) and len(result) == 1:
        only = next(iter(result.values()))
        if isinstance(only, (str, int, float, bool)):
            return str(only)
    return json.dumps(result, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------


def dispatch(tool_name: str, str_args: list[str]) -> int:
    """Run `tool_name(*str_args)` and print the formatted result.

    Returns 0 on success, 1 on tool error, 2 on usage error (unknown
    tool, wrong arity). Diagnostics go to stderr; the formatted
    result (if any) goes to stdout."""
    if tool_name not in _valid_tool_names():
        print(f"holo: unknown tool {tool_name!r}", file=sys.stderr)
        return 2

    fn = getattr(server, tool_name)

    try:
        # eval_str=True so `from __future__ import annotations` in
        # server.py doesn't leave annotations as bare strings —
        # _coerce needs real type objects to dispatch correctly.
        sig = inspect.signature(fn, eval_str=True)
    except (TypeError, ValueError, NameError) as e:
        print(f"holo: cannot introspect {tool_name}: {e}", file=sys.stderr)
        return 2

    params = list(sig.parameters.values())
    last_idx = len(params) - 1
    bound: list[Any] = []
    consumed = 0

    for i, p in enumerate(params):
        stripped, _ = _strip_optional(p.annotation)

        # Trailing list[T] param + remaining args -> slurp all remaining
        # shell words into a list. Matches the SHORT_DOC convention
        # `[MODIFIER...]` for varargs-style tools.
        if i == last_idx and _is_list_annotation(stripped) and i < len(str_args):
            try:
                bound.append(_coerce_list_slurp(str_args[i:], stripped))
            except (ValueError, TypeError) as e:
                print(f"holo: bad list arg for {tool_name}: {e}",
                      file=sys.stderr)
                return 2
            consumed = len(str_args)
            continue

        if i < len(str_args):
            try:
                bound.append(_coerce(str_args[i], p.annotation))
            except (ValueError, TypeError) as e:
                print(f"holo: bad arg {i + 1} for {tool_name}: {e}",
                      file=sys.stderr)
                return 2
            consumed = i + 1
        elif p.default is not inspect.Parameter.empty:
            bound.append(p.default)
        else:
            print(f"holo: {tool_name} requires {p.name}", file=sys.stderr)
            return 2

    if consumed < len(str_args):
        extra = len(str_args) - consumed
        print(f"holo: {tool_name} got {extra} extra arg(s)", file=sys.stderr)
        return 2

    try:
        result = fn(*bound)
    except Exception as e:
        print(f"holo: {tool_name}: {e}", file=sys.stderr)
        return 1

    if result is None:
        return 0
    print(_format(result))
    return 0
