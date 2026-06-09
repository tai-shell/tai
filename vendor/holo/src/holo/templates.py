"""Template cache for desktop UI elements.

Pure-Python store for `(app, label) → list[png variant file]` plus
metadata. SikuliX template-matching code lives in `holo.bridge` and the
JVM-side `bridge/bridge.py`; this module only handles the on-disk
layout, the `index.json`, and label/app validation.

Layout:

    <root>/
      index.json
      _global/
        apple_menu.png
      chrome/
        kebab.png
        kebab_hover.png

Default root: `$HOLO_TEMPLATE_DIR`, else `<user cache>/holo/templates`,
where the user-cache part follows the same XDG-vs-macOS rules as
`holo.bridge._user_cache_dir`.

Index entries (one per (app, label)):

    {
      "app": "chrome",
      "label": "kebab",
      "variants": ["kebab.png", "kebab_hover.png"],
      "similarity": 0.85,
      "w": 24, "h": 24,
      "created": "2026-04-30T20:50:00Z",
      "last_used": "2026-04-30T21:12:00Z",
      "match_count": 7
    }
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GLOBAL_APP = "_global"

# Labels and app keys are used as path segments. Restrict to a safe ASCII
# subset rather than relying on filesystem rules — no slashes, no leading
# dots, no whitespace surprises.
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


class TemplateError(ValueError):
    """Bad input to the store (e.g. invalid label/app, empty PNG)."""


class TemplateNotFound(LookupError):
    """No matching index entry. Carries `app` and `label` for messages."""

    def __init__(self, label: str, app: str) -> None:
        super().__init__(f"no template registered for {app}/{label}")
        self.label = label
        self.app = app


def default_root() -> Path:
    """Resolve the default template root.

    Honors `HOLO_TEMPLATE_DIR`. Otherwise mirrors the cache directory
    convention used by `holo.bridge` (XDG on Linux/cross-platform,
    `~/Library/Caches/holo` on macOS).
    """
    override = os.environ.get("HOLO_TEMPLATE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "holo" / "templates"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "holo" / "templates"
    return Path.home() / ".cache" / "holo" / "templates"


def _validate_name(value: str, kind: str) -> None:
    if not isinstance(value, str) or not value:
        raise TemplateError(f"{kind} must be a non-empty string")
    if not _NAME_RE.match(value):
        raise TemplateError(
            f"{kind} {value!r} must match {_NAME_RE.pattern} "
            "(ASCII letters/digits/_.-, no slashes, max 64 chars)"
        )


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG's IHDR chunk. No Pillow needed."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise TemplateError("not a valid PNG (missing signature)")
    # IHDR is the first chunk: 4-byte length, "IHDR", then width/height
    # as big-endian uint32.
    if data[12:16] != b"IHDR":
        raise TemplateError("not a valid PNG (no IHDR chunk)")
    w, h = struct.unpack(">II", data[16:24])
    return int(w), int(h)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TemplateStore:
    """Thread-safe on-disk template index. All public methods take a lock."""

    INDEX_NAME = "index.json"
    INDEX_VERSION = 1

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_root()).expanduser()
        self._lock = threading.RLock()

    # ---- key / path helpers --------------------------------------------

    @staticmethod
    def _normalize_app(app: str | None) -> str:
        if app is None or app == "":
            return GLOBAL_APP
        _validate_name(app, "app")
        return app

    @staticmethod
    def _key(app: str, label: str) -> str:
        return f"{app}/{label}"

    def _app_dir(self, app: str) -> Path:
        return self.root / app

    def _index_path(self) -> Path:
        return self.root / self.INDEX_NAME

    # ---- index I/O -----------------------------------------------------

    def _load_index(self) -> dict[str, Any]:
        path = self._index_path()
        if not path.exists():
            return {"version": self.INDEX_VERSION, "templates": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise TemplateError(f"corrupt index at {path}: {e}") from e
        if not isinstance(data, dict) or "templates" not in data:
            raise TemplateError(f"index at {path} has no `templates` map")
        # Forward-compat: store the version we last saw, but don't reject
        # newer versions outright — let callers fail on missing fields.
        data.setdefault("version", self.INDEX_VERSION)
        if not isinstance(data["templates"], dict):
            raise TemplateError(f"index at {path}: `templates` is not a map")
        return data

    def _write_index(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._index_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    # ---- public API ----------------------------------------------------

    def add_variant(
        self,
        label: str,
        app: str | None,
        png_bytes: bytes,
        *,
        replace: bool = False,
        similarity: float = 0.85,
    ) -> dict[str, Any]:
        """Save `png_bytes` as a variant under (app, label).

        - `replace=False` (default): append a new variant. New file is
          named `<label>.png` if free, else `<label>_2.png`, `_3.png`, …
        - `replace=True`: discard any existing variants for the entry,
          delete their files, write only this one.

        Returns the index entry (mutated). Raises `TemplateError` on
        bad inputs (invalid label/app, non-PNG bytes).
        """
        _validate_name(label, "label")
        app_norm = self._normalize_app(app)
        if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes:
            raise TemplateError("png_bytes must be non-empty bytes")
        w, h = _png_dimensions(bytes(png_bytes))
        if not (0.0 < similarity <= 1.0):
            raise TemplateError("similarity must be in (0, 1]")

        with self._lock:
            data = self._load_index()
            key = self._key(app_norm, label)
            entry = data["templates"].get(key)
            self._app_dir(app_norm).mkdir(parents=True, exist_ok=True)

            if entry is None or replace:
                # Wipe any prior on-disk variants when replacing — the
                # entry's `variants` list is the source of truth for what
                # we own under <app>/.
                if entry is not None and replace:
                    for v in entry.get("variants", []):
                        self._unlink_quietly(self._app_dir(app_norm) / v)
                entry = {
                    "app": app_norm,
                    "label": label,
                    "variants": [],
                    "similarity": float(similarity),
                    "w": w,
                    "h": h,
                    "created": _now_iso(),
                    "last_used": None,
                    "match_count": 0,
                }
                data["templates"][key] = entry

            # Pick a free filename for the new variant.
            new_name = self._next_variant_name(label, entry["variants"])
            (self._app_dir(app_norm) / new_name).write_bytes(bytes(png_bytes))
            entry["variants"].append(new_name)
            # `similarity` lives at the entry level, not per-variant.
            # Last-write wins for an existing entry (when caller passes
            # an explicit value).
            entry["similarity"] = float(similarity)
            self._write_index(data)
            # Return a copy so callers can't mutate cached state.
            return dict(entry)

    def get(self, label: str, app: str | None) -> dict[str, Any] | None:
        _validate_name(label, "label")
        app_norm = self._normalize_app(app)
        with self._lock:
            data = self._load_index()
            entry = data["templates"].get(self._key(app_norm, label))
            return dict(entry) if entry else None

    def variant_paths(self, label: str, app: str | None) -> list[Path]:
        """Absolute paths of every variant for (app, label), in order.

        Raises `TemplateNotFound` if there's no entry. Filters out any
        variant whose file is missing on disk (the caller decides whether
        that's an error).
        """
        entry = self.get(label, app)
        if entry is None:
            raise TemplateNotFound(label, self._normalize_app(app))
        app_dir = self._app_dir(entry["app"])
        return [app_dir / v for v in entry["variants"] if (app_dir / v).exists()]

    def list(self, app: str | None = None) -> list[dict[str, Any]]:
        """List index entries. `app=None` → all apps; `app="chrome"` → just chrome.

        Pass `app="_global"` (or `holo.templates.GLOBAL_APP`) to filter
        to the catch-all bucket.
        """
        with self._lock:
            data = self._load_index()
            entries = list(data["templates"].values())
        if app is not None:
            app_norm = self._normalize_app(app)
            entries = [e for e in entries if e.get("app") == app_norm]
        # Deterministic order: app then label.
        entries.sort(key=lambda e: (e.get("app", ""), e.get("label", "")))
        return [dict(e) for e in entries]

    def touch(
        self,
        label: str,
        app: str | None,
        *,
        when: str | None = None,
        increment: bool = True,
    ) -> None:
        """Bump `last_used` and (by default) `match_count` after a hit."""
        _validate_name(label, "label")
        app_norm = self._normalize_app(app)
        with self._lock:
            data = self._load_index()
            entry = data["templates"].get(self._key(app_norm, label))
            if entry is None:
                return
            entry["last_used"] = when or _now_iso()
            if increment:
                entry["match_count"] = int(entry.get("match_count", 0)) + 1
            self._write_index(data)

    def delete(
        self,
        label: str,
        app: str | None,
        *,
        variant: str | None = None,
    ) -> list[str]:
        """Remove an entry, or just one variant from it.

        Returns the names of files actually removed from disk.
        Deleting the last variant removes the whole entry.
        """
        _validate_name(label, "label")
        app_norm = self._normalize_app(app)
        with self._lock:
            data = self._load_index()
            key = self._key(app_norm, label)
            entry = data["templates"].get(key)
            if entry is None:
                return []
            removed: list[str] = []
            app_dir = self._app_dir(app_norm)
            if variant is None:
                # Remove all variants + entry.
                for v in entry["variants"]:
                    if self._unlink_quietly(app_dir / v):
                        removed.append(v)
                del data["templates"][key]
            else:
                if variant not in entry["variants"]:
                    raise TemplateError(
                        f"variant {variant!r} not in {app_norm}/{label}"
                    )
                if self._unlink_quietly(app_dir / variant):
                    removed.append(variant)
                entry["variants"].remove(variant)
                if not entry["variants"]:
                    # No variants left → drop the whole entry.
                    del data["templates"][key]
            self._write_index(data)
            return removed

    # ---- internals -----------------------------------------------------

    @staticmethod
    def _next_variant_name(label: str, existing: list[str]) -> str:
        if f"{label}.png" not in existing:
            return f"{label}.png"
        i = 2
        while f"{label}_{i}.png" in existing:
            i += 1
        return f"{label}_{i}.png"

    @staticmethod
    def _unlink_quietly(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False
