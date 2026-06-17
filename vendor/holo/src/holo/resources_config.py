"""TOML-backed resource declarations for ``holo mcp``.

The CLI flag ``--announce-resource`` is convenient for one-off
demos. Any daemon running for more than a few minutes wants its
resources declared in a config file — a single source of truth that
survives restarts, fits in source control, and reviews cleanly.

This module loads that file. Format is TOML (stdlib ``tomllib``,
Python 3.11+) at ``~/.config/holo/resources.toml`` by default, or
wherever ``--resources-config PATH`` points.

The :class:`Resource` schema is the same as the CLI form; the loader
is a thin parse + validate over what
:func:`holo.announce.parse_resource_spec` accepts, just with the
ergonomics of nested config sections instead of one packed string.

The Phase 0 design doc named ``~/.holo/resources.yml`` for this
config. We diverged to TOML because:

  1. ``tomllib`` is stdlib in 3.11+ (holo's minimum), avoiding a
     PyYAML dependency for what's a small config surface.
  2. ``~/.config/holo/`` is already where the cert/key state lives
     (see :mod:`holo.cert`), so resource config alongside it is
     consistent.

Example::

    # ~/.config/holo/resources.toml

    [resources.movies]
    path = "/Volumes/movies"
    tags = ["video-files", "archive"]
    caps = ["exec:ffprobe", "exec:python3", "readonly"]
    allow_principals = ["alice@laptop", "*@home-lan"]

    [resources.private-photos]
    path = "/Users/me/Photos"
    tags = ["photos"]
    caps = ["exec:ffprobe"]
    allow_principals = ["alice@laptop"]

Note: ``allow_principals`` is **not enforced in v1** — see the
:class:`holo.announce.Resource` docstring for the why.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from holo.announce import Resource

# Default location. ``~/.config/holo/`` matches where ``holo cert``
# already keeps the keypair + cert + meta — putting resources alongside
# keeps the daemon's config story in one place.
DEFAULT_CONFIG_PATH = Path(
    os.path.expanduser("~/.config/holo/resources.toml")
)


# Allowed keys per resource. Mirror :func:`parse_resource_spec`'s key
# set so a TOML config and a CLI flag accept the same fields.
_ALLOWED_KEYS = frozenset({"path", "tags", "caps", "allow_principals"})


class ResourcesConfigError(Exception):
    """Raised when a resources TOML file is malformed.

    Message names the offending key and what was expected — the CLI
    surfaces this verbatim so the user can fix the config and retry.
    """


def load_resources_toml(path: Path | str) -> list[Resource]:
    """Parse ``path`` as TOML and return its resources as :class:`Resource` list.

    Raises :class:`ResourcesConfigError` on:

      - file missing / not readable
      - not valid TOML
      - missing top-level ``[resources]`` table (empty list returned
        only when ``[resources]`` exists but is empty)
      - any per-resource entry missing ``path`` or naming an unknown
        key

    Raises :class:`ValueError` (from :class:`Resource` validation) on
    a syntactically valid entry whose values fail the Resource invariants
    (empty tag, comma in caps, etc.).

    Order of returned resources matches TOML's insertion order, so a
    file authored with a deliberate ordering keeps it for the daemon's
    announce-time iteration.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError as e:
        raise ResourcesConfigError(
            f"resources config not found at {p}"
        ) from e
    except OSError as e:
        raise ResourcesConfigError(
            f"resources config {p} unreadable: {e}"
        ) from e

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ResourcesConfigError(
            f"resources config {p} is not valid UTF-8: {e}"
        ) from e
    except tomllib.TOMLDecodeError as e:
        raise ResourcesConfigError(
            f"resources config {p} is not valid TOML: {e}"
        ) from e

    if "resources" not in data:
        raise ResourcesConfigError(
            f"resources config {p}: missing top-level [resources] table "
            "(define resources as [resources.NAME])"
        )
    resources_table = data["resources"]
    if not isinstance(resources_table, dict):
        raise ResourcesConfigError(
            f"resources config {p}: [resources] must be a table of "
            f"NAME = {{...}} entries, got {type(resources_table).__name__}"
        )

    out: list[Resource] = []
    for name, entry in resources_table.items():
        if not isinstance(entry, dict):
            raise ResourcesConfigError(
                f"resources config {p}: [resources.{name}] must be a "
                f"table, got {type(entry).__name__}"
            )
        unknown = set(entry) - _ALLOWED_KEYS
        if unknown:
            raise ResourcesConfigError(
                f"resources config {p}: [resources.{name}] has unknown "
                f"keys {sorted(unknown)} (allowed: {sorted(_ALLOWED_KEYS)})"
            )
        if "path" not in entry:
            raise ResourcesConfigError(
                f"resources config {p}: [resources.{name}] missing "
                "required key 'path'"
            )
        for list_key in ("tags", "caps", "allow_principals"):
            if list_key in entry:
                value = entry[list_key]
                if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in value
                ):
                    raise ResourcesConfigError(
                        f"resources config {p}: [resources.{name}].{list_key} "
                        f"must be a list of strings, got {value!r}"
                    )
        try:
            out.append(
                Resource(
                    name=name,
                    path=entry["path"],
                    tags=tuple(entry.get("tags", ())),
                    caps=tuple(entry.get("caps", ())),
                    allow_principals=tuple(entry.get("allow_principals", ())),
                )
            )
        except ValueError as e:
            raise ResourcesConfigError(
                f"resources config {p}: [resources.{name}]: {e}"
            ) from e
    return out


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ResourcesConfigError",
    "load_resources_toml",
]
