"""Tests for holo.windows.

The macOS implementation calls into Quartz, which we can't exercise
deterministically in CI (Screen Recording permission, headless runner
state, etc.). Instead, we exercise the parse + filter logic directly
against fixture dicts shaped like CGWindowListCopyWindowInfo entries,
and we mock the Quartz call to verify the integration path.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from holo._windows_macos import _is_visible, _parse, list_windows
from holo.windows import WindowInfo


def _entry(**overrides):
    base = {
        "kCGWindowNumber": 42,
        "kCGWindowName": "Example - Chrome",
        "kCGWindowOwnerName": "Google Chrome",
        "kCGWindowOwnerPID": 1234,
        "kCGWindowLayer": 0,
        "kCGWindowAlpha": 1.0,
        "kCGWindowBounds": {"X": 100, "Y": 50, "Width": 800, "Height": 600},
    }
    base.update(overrides)
    return base


def test_parse_full_entry():
    info = _parse(_entry())
    assert info == WindowInfo(
        id=42,
        title="Example - Chrome",
        owner="Google Chrome",
        layer=0,
        pid=1234,
        bounds=(100.0, 50.0, 800.0, 600.0),
    )


def test_parse_missing_bounds_yields_none():
    info = _parse(_entry(kCGWindowBounds=None))
    assert info.bounds is None


def test_parse_malformed_bounds_yields_none():
    info = _parse(_entry(kCGWindowBounds={"X": 1}))  # missing Y/Width/Height
    assert info.bounds is None


def test_parse_none_title_yields_empty_string():
    info = _parse(_entry(kCGWindowName=None))
    assert info.title == ""


def test_parse_missing_fields_use_defaults():
    info = _parse({})
    assert info.id == 0
    assert info.title == ""
    assert info.owner == ""
    assert info.layer == 0
    assert info.pid == 0


def test_is_visible_filters_zero_alpha():
    assert _is_visible(_entry(kCGWindowAlpha=1.0)) is True
    assert _is_visible(_entry(kCGWindowAlpha=0.5)) is True
    assert _is_visible(_entry(kCGWindowAlpha=0)) is False
    assert _is_visible(_entry(kCGWindowAlpha=0.0)) is False


def test_is_visible_missing_alpha_treated_as_invisible():
    entry = _entry()
    del entry["kCGWindowAlpha"]
    assert _is_visible(entry) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only path")
def test_list_windows_uses_quartz_and_filters_invisible():
    """list_windows() must call CGWindowListCopyWindowInfo and drop alpha-0 entries."""
    fake = [
        _entry(kCGWindowNumber=1, kCGWindowName="Visible"),
        _entry(kCGWindowNumber=2, kCGWindowName="Hidden", kCGWindowAlpha=0),
        _entry(kCGWindowNumber=3, kCGWindowName="AlsoVisible"),
    ]
    with patch("Quartz.CGWindowListCopyWindowInfo", return_value=fake) as m:
        windows = list_windows()
    assert m.called
    ids = [w.id for w in windows]
    assert ids == [1, 3]


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only path")
def test_list_windows_handles_none_return():
    """Quartz returns None when no windows match; list_windows must not crash."""
    with patch("Quartz.CGWindowListCopyWindowInfo", return_value=None):
        assert list_windows() == []


def test_public_api_raises_on_unsupported_platform():
    """Non-darwin platforms get a clear NotImplementedError, not an import error."""
    from holo import windows

    with patch.object(windows.sys, "platform", "linux"):
        with pytest.raises(NotImplementedError, match="linux"):
            windows.list_windows()
