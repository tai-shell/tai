"""MCP client for talking to a holo daemon over stdio.

Phase 3.A v1: spawn ``<holo_command> mcp`` as a subprocess per
dispatch target and speak MCP over its stdin/stdout. Validates the
client-side surface for ``holo_list_resources`` and
``holo_exec_in_resource`` end-to-end.

What this DOESN'T do (intentional Phase 3.A scope):

- No auto-tunnel transport (the production path for remote daemons).
  The 3.B+ wiring uses the existing ``holo tunnel up`` setup and
  reaches the daemon's MCP through the tunnel. For v1 of the
  dispatch runtime, local stdio is enough to validate the client
  surface; the transport story is independent.

- No connection pooling. Each MCP call spawns a fresh ``holo mcp``
  subprocess. That's wasteful (200ms/call setup), fine for the
  CLI smoke / unit tests. 3.B+ keeps one connection per daemon
  per shell-lifetime as the Phase 0 doc Q6 prescribes.

- No FastMCP server-side feature consumption (resources/prompts/etc.).
  Only tool-call.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

_log = logging.getLogger(__name__)


# Holo MCP tools used in this module.
TOOL_LIST_RESOURCES = "holo_list_resources"
TOOL_EXEC_IN_RESOURCE = "holo_exec_in_resource"


class HoloMCPClientError(Exception):
    """Raised when the daemon returns a structured error or the
    transport itself fails before the call completes."""


@asynccontextmanager
async def _open_session(
    holo_command: str,
    extra_args: list[str] | None = None,
) -> AsyncIterator[Any]:
    """Spawn ``<holo_command> mcp [extra_args]`` and yield a
    :class:`mcp.ClientSession`.

    ``extra_args`` lets the caller pass ``--announce``,
    ``--resources-config PATH``, etc. — the same flags ``holo mcp``
    accepts. In production these would already be set by whoever
    launched the daemon; here we let the test harness configure
    them per-call.

    The session is initialised before being yielded; the caller can
    immediately ``call_tool``. On exit, the subprocess is terminated
    cleanly.
    """
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    args = ["mcp"] + (extra_args or [])
    params = StdioServerParameters(command=holo_command, args=args)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_resources(
    holo_command: str,
    *,
    extra_args: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Call ``holo_list_resources`` on the daemon and return the list.

    The shape is exactly what :func:`holo.mcp_server.HoloMCPServer.list_resources`
    returns: a list of dicts with ``name``, ``path``, ``tags``,
    ``caps``, ``allow_principals`` per resource.

    Raises :class:`HoloMCPClientError` if the daemon doesn't expose
    the tool (it's conditional on having any declared resources) or
    if the call fails at the transport layer.
    """
    async with _open_session(holo_command, extra_args) as session:
        try:
            result = await session.call_tool(TOOL_LIST_RESOURCES, {})
        except Exception as e:
            raise HoloMCPClientError(
                f"holo_list_resources via {holo_command!r}: {e}"
            ) from e
    body = _extract_structured(result)
    if not isinstance(body, dict) or "resources" not in body:
        raise HoloMCPClientError(
            f"holo_list_resources via {holo_command!r}: unexpected "
            f"response shape {body!r}"
        )
    return body["resources"]


async def exec_in_resource(
    holo_command: str,
    resource: str,
    body: str,
    *,
    env: dict[str, str] | None = None,
    timeout_s: int = 60,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Call ``holo_exec_in_resource`` and return the structured response.

    Successful response shape::

        {"frames": [{"fd": "stdout"|"stderr", "data": str}, ...],
         "exit": int, "duration_ms": int, "timed_out": bool}

    Failure modes from the daemon are returned IN the response with
    a top-level ``"error"`` key (see ``HoloMCPServer.exec_in_resource``
    in holo) — the caller decides whether to surface them or treat as
    a non-zero exit. Only transport / serialization failures raise
    :class:`HoloMCPClientError`.
    """
    args = {
        "resource": resource,
        "body": body,
        "env": env or {},
        "timeout_s": timeout_s,
    }
    async with _open_session(holo_command, extra_args) as session:
        try:
            result = await session.call_tool(TOOL_EXEC_IN_RESOURCE, args)
        except Exception as e:
            raise HoloMCPClientError(
                f"holo_exec_in_resource({resource!r}) via "
                f"{holo_command!r}: {e}"
            ) from e
    body_out = _extract_structured(result)
    if not isinstance(body_out, dict):
        raise HoloMCPClientError(
            f"holo_exec_in_resource: unexpected response shape "
            f"{body_out!r}"
        )
    return body_out


def _extract_structured(result: Any) -> Any:
    """Pull the structured body off a CallToolResult.

    The mcp library returns a CallToolResult with both human-readable
    text content and a parsed structured content. We always want the
    structured form — it's the dict the daemon returned, untouched by
    JSON-string round-tripping.
    """
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    # Older library versions return a (content, structured) tuple
    if isinstance(result, tuple) and len(result) == 2:
        return result[1]
    return None


__all__ = [
    "HoloMCPClientError",
    "TOOL_EXEC_IN_RESOURCE",
    "TOOL_LIST_RESOURCES",
    "exec_in_resource",
    "list_resources",
]
