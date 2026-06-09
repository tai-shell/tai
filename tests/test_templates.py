"""Tests for `holo.templates` — the on-disk template cache.

These exercise the index I/O, validation, and atomic update logic with
no JVM and no display. Build minimal PNG bytes inline so the suite stays
self-contained.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from holo.templates import (
    GLOBAL_APP,
    TemplateError,
    TemplateNotFound,
    TemplateStore,
    _png_dimensions,
    default_root,
)

# ---- fixtures -------------------------------------------------------


def _make_png(width: int, height: int) -> bytes:
    """Hand-roll a tiny valid RGB PNG (one solid black pixel grid)."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(typ: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(typ + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # Filter byte 0 + RGB triplets, all zero.
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    idat = zlib.compress(raw)
    return (
        sig
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


@pytest.fixture
def store(tmp_path: Path) -> TemplateStore:
    return TemplateStore(root=tmp_path / "templates")


@pytest.fixture
def png24() -> bytes:
    return _make_png(24, 24)


@pytest.fixture
def png16() -> bytes:
    return _make_png(16, 16)


# ---- _png_dimensions -----------------------------------------------


class TestPngDimensions:
    def test_reads_width_and_height(self, png24):
        assert _png_dimensions(png24) == (24, 24)

    def test_rejects_non_png_bytes(self):
        with pytest.raises(TemplateError, match="signature"):
            _png_dimensions(b"this is clearly not a PNG")

    def test_rejects_too_short(self):
        with pytest.raises(TemplateError):
            _png_dimensions(b"\x89PNG\r\n\x1a\n")

    def test_rejects_missing_ihdr(self):
        # Valid signature but garbage chunk header.
        bad = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        with pytest.raises(TemplateError, match="IHDR"):
            _png_dimensions(bad)


# ---- default_root --------------------------------------------------


class TestDefaultRoot:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOLO_TEMPLATE_DIR", str(tmp_path / "custom"))
        assert default_root() == tmp_path / "custom"

    def test_xdg_cache_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HOLO_TEMPLATE_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert default_root() == tmp_path / "xdg" / "holo" / "templates"

    def test_macos_default(self, monkeypatch):
        monkeypatch.delenv("HOLO_TEMPLATE_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr("holo.templates.sys.platform", "darwin")
        root = default_root()
        # Don't assert the literal home path; just the suffix structure.
        assert root.parts[-3:] == ("Caches", "holo", "templates")


# ---- validation ----------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("bad", ["", "  ", "has space", "a/b", "..", "x" * 65])
    def test_rejects_bad_label(self, store, png24, bad):
        with pytest.raises(TemplateError, match="label"):
            store.add_variant(bad, "chrome", png24)

    @pytest.mark.parametrize("bad", ["with space", "../etc/passwd", "a/b"])
    def test_rejects_bad_app(self, store, png24, bad):
        with pytest.raises(TemplateError, match="app"):
            store.add_variant("kebab", bad, png24)

    def test_rejects_empty_png_bytes(self, store):
        with pytest.raises(TemplateError, match="png_bytes"):
            store.add_variant("kebab", "chrome", b"")

    def test_rejects_non_png_bytes(self, store):
        with pytest.raises(TemplateError, match="PNG"):
            store.add_variant("kebab", "chrome", b"not a png")

    @pytest.mark.parametrize("bad", [0, -0.1, 1.1, 2.0])
    def test_rejects_out_of_range_similarity(self, store, png24, bad):
        with pytest.raises(TemplateError, match="similarity"):
            store.add_variant("kebab", "chrome", png24, similarity=bad)


# ---- add_variant ---------------------------------------------------


class TestAddVariant:
    def test_creates_first_variant(self, store, png24):
        entry = store.add_variant("kebab", "chrome", png24, similarity=0.9)
        assert entry["app"] == "chrome"
        assert entry["label"] == "kebab"
        assert entry["variants"] == ["kebab.png"]
        assert entry["w"] == 24
        assert entry["h"] == 24
        assert entry["similarity"] == 0.9
        assert entry["match_count"] == 0
        assert entry["last_used"] is None
        # File and index actually exist on disk.
        assert (store.root / "chrome" / "kebab.png").exists()
        index = json.loads((store.root / "index.json").read_text())
        assert "chrome/kebab" in index["templates"]

    def test_app_none_routes_to_global_bucket(self, store, png24):
        entry = store.add_variant("dock", None, png24)
        assert entry["app"] == GLOBAL_APP
        assert (store.root / GLOBAL_APP / "dock.png").exists()

    def test_appending_variants(self, store, png24, png16):
        store.add_variant("kebab", "chrome", png24)
        entry = store.add_variant("kebab", "chrome", png16)
        assert entry["variants"] == ["kebab.png", "kebab_2.png"]
        # Width/height from the FIRST variant — we don't update on append
        # since variants can legitimately differ.
        assert entry["w"] == 24

    def test_appending_picks_next_free_slot(self, store, png24):
        store.add_variant("kebab", "chrome", png24)
        store.add_variant("kebab", "chrome", png24)
        store.add_variant("kebab", "chrome", png24)
        entry = store.get("kebab", "chrome")
        assert entry["variants"] == ["kebab.png", "kebab_2.png", "kebab_3.png"]

    def test_replace_wipes_prior_files_and_index(self, store, png24, png16):
        store.add_variant("kebab", "chrome", png24)
        store.add_variant("kebab", "chrome", png16)
        # Now replace.
        entry = store.add_variant("kebab", "chrome", png24, replace=True)
        assert entry["variants"] == ["kebab.png"]
        # Old `_2` file is gone.
        assert not (store.root / "chrome" / "kebab_2.png").exists()
        # Created timestamp resets on replace.
        assert entry["match_count"] == 0

    def test_similarity_last_write_wins_on_append(self, store, png24):
        store.add_variant("kebab", "chrome", png24, similarity=0.7)
        entry = store.add_variant("kebab", "chrome", png24, similarity=0.95)
        assert entry["similarity"] == 0.95


# ---- get / variant_paths / list ------------------------------------


class TestRead:
    def test_get_returns_none_for_missing(self, store):
        assert store.get("missing", "chrome") is None

    def test_variant_paths_raises_for_missing(self, store):
        with pytest.raises(TemplateNotFound) as exc:
            store.variant_paths("missing", "chrome")
        assert exc.value.label == "missing"
        assert exc.value.app == "chrome"

    def test_variant_paths_filters_missing_files(self, store, png24):
        store.add_variant("kebab", "chrome", png24)
        store.add_variant("kebab", "chrome", png24)
        # User deleted one PNG out from under us.
        (store.root / "chrome" / "kebab.png").unlink()
        paths = store.variant_paths("kebab", "chrome")
        assert len(paths) == 1
        assert paths[0].name == "kebab_2.png"

    def test_list_all(self, store, png24):
        store.add_variant("a", "chrome", png24)
        store.add_variant("b", "slack", png24)
        store.add_variant("c", None, png24)
        out = store.list()
        assert {(e["app"], e["label"]) for e in out} == {
            ("chrome", "a"),
            ("slack", "b"),
            (GLOBAL_APP, "c"),
        }

    def test_list_filtered_by_app(self, store, png24):
        store.add_variant("a", "chrome", png24)
        store.add_variant("b", "slack", png24)
        out = store.list(app="chrome")
        assert [e["label"] for e in out] == ["a"]

    def test_list_global_filter(self, store, png24):
        store.add_variant("dock", None, png24)
        store.add_variant("kebab", "chrome", png24)
        out = store.list(app=GLOBAL_APP)
        assert [e["label"] for e in out] == ["dock"]

    def test_empty_store_returns_empty_list(self, store):
        assert store.list() == []


# ---- touch / delete -------------------------------------------------


class TestMutate:
    def test_touch_updates_last_used_and_count(self, store, png24):
        store.add_variant("kebab", "chrome", png24)
        store.touch("kebab", "chrome", when="2026-04-30T20:00:00Z")
        store.touch("kebab", "chrome", when="2026-04-30T20:01:00Z")
        entry = store.get("kebab", "chrome")
        assert entry["last_used"] == "2026-04-30T20:01:00Z"
        assert entry["match_count"] == 2

    def test_touch_missing_is_noop(self, store):
        # Should not raise.
        store.touch("nope", "chrome")

    def test_delete_whole_entry(self, store, png24):
        store.add_variant("kebab", "chrome", png24)
        store.add_variant("kebab", "chrome", png24)
        removed = store.delete("kebab", "chrome")
        assert sorted(removed) == ["kebab.png", "kebab_2.png"]
        assert store.get("kebab", "chrome") is None
        # Files gone.
        assert not (store.root / "chrome" / "kebab.png").exists()

    def test_delete_one_variant(self, store, png24):
        store.add_variant("kebab", "chrome", png24)
        store.add_variant("kebab", "chrome", png24)
        removed = store.delete("kebab", "chrome", variant="kebab.png")
        assert removed == ["kebab.png"]
        entry = store.get("kebab", "chrome")
        assert entry["variants"] == ["kebab_2.png"]

    def test_delete_last_variant_drops_entry(self, store, png24):
        store.add_variant("kebab", "chrome", png24)
        store.delete("kebab", "chrome", variant="kebab.png")
        assert store.get("kebab", "chrome") is None

    def test_delete_unknown_variant_raises(self, store, png24):
        store.add_variant("kebab", "chrome", png24)
        with pytest.raises(TemplateError, match="variant"):
            store.delete("kebab", "chrome", variant="nope.png")

    def test_delete_missing_entry_returns_empty(self, store):
        assert store.delete("missing", "chrome") == []


# ---- index resilience ---------------------------------------------


class TestIndexFormat:
    def test_corrupt_index_raises(self, tmp_path):
        root = tmp_path / "templates"
        root.mkdir()
        (root / "index.json").write_text("not json{")
        store = TemplateStore(root=root)
        with pytest.raises(TemplateError, match="corrupt"):
            store.list()

    def test_index_missing_templates_key_raises(self, tmp_path):
        root = tmp_path / "templates"
        root.mkdir()
        (root / "index.json").write_text(json.dumps({"version": 1}))
        store = TemplateStore(root=root)
        with pytest.raises(TemplateError, match="templates"):
            store.list()

    def test_atomic_write_no_partial_index(self, store, png24):
        store.add_variant("a", "chrome", png24)
        # Tmp file should not linger.
        assert not (store.root / "index.json.tmp").exists()
        # Index should parse.
        json.loads((store.root / "index.json").read_text())

    def test_root_created_lazily(self, tmp_path, png24):
        root = tmp_path / "deep" / "templates"
        store = TemplateStore(root=root)
        assert not root.exists()
        store.add_variant("a", "chrome", png24)
        assert root.exists()
        assert (root / "chrome" / "a.png").exists()
