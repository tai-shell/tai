"""Tests for holo.title — Python mirror of bookmarklet/title.js."""

from __future__ import annotations

import base64

import pytest

from holo.title import decode_framed, decode_plain, is_holo_title


def _framed(json_str: str, original: str = "Page") -> str:
    encoded = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
    return f"{original} [holo:1:{encoded}]" if original else f"[holo:1:{encoded}]"


def _plain(marker: str, original: str = "Page") -> str:
    return f"{original} [holo:{marker}]" if original else f"[holo:{marker}]"


class TestDecodeFramed:
    def test_round_trip(self):
        json_str = '{"v":1,"session":"s","type":"cmd"}'
        assert decode_framed(_framed(json_str)) == json_str

    def test_works_without_original_title(self):
        json_str = '{"v":1}'
        assert decode_framed(_framed(json_str, original="")) == json_str

    def test_returns_none_for_titles_without_marker(self):
        assert decode_framed("Plain page title") is None
        assert decode_framed("") is None

    def test_returns_none_for_non_string_inputs(self):
        assert decode_framed(None) is None
        assert decode_framed(42) is None
        assert decode_framed(b"bytes") is None

    def test_tolerates_trailing_whitespace(self):
        assert decode_framed(_framed("{}") + "   ") == "{}"

    def test_returns_none_for_invalid_base64(self):
        assert decode_framed("page [holo:1:!!!notbase64!!!]") is None


class TestDecodePlain:
    def test_round_trip(self):
        assert decode_plain(_plain("cal:abc-123")) == "cal:abc-123"

    def test_returns_none_for_framed_payload(self):
        assert decode_plain(_framed("{}")) is None

    def test_returns_none_for_titles_without_marker(self):
        assert decode_plain("Just a regular title") is None

    def test_returns_none_for_non_string_inputs(self):
        assert decode_plain(None) is None
        assert decode_plain(42) is None


class TestIsHoloTitle:
    def test_recognizes_framed(self):
        assert is_holo_title(_framed("{}")) is True

    def test_recognizes_plain(self):
        assert is_holo_title(_plain("cal:1")) is True

    def test_rejects_regular_titles(self):
        assert is_holo_title("Just a page title") is False
        assert is_holo_title("") is False


class TestInteropWithJsBookmarklet:
    """The wire format must match bookmarklet/title.js byte-for-byte.

    The JS encoder produces `<orig> [holo:1:<base64(json)>]`. We verify
    the Python decoder accepts that exact shape.
    """

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("GitHub - Mozilla Firefox [holo:1:e30=]", "{}"),
            ("[holo:1:eyJ4IjogMX0=]", '{"x": 1}'),
            ("page [holo:1:eyJzZXNzaW9uIjoiYWJjIn0=]", '{"session":"abc"}'),
        ],
    )
    def test_decodes_js_produced_titles(self, title, expected):
        assert decode_framed(title) == expected


class TestBrowserSuffix:
    """OS-level window titles from browsers append a suffix like
    " - Google Chrome" or " — Mozilla Firefox" after document.title.
    The marker is then in the middle of the string, not the end.
    """

    def test_decode_framed_finds_marker_before_browser_suffix(self):
        json_str = '{"v":1}'
        encoded = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
        title = f"My Page [holo:1:{encoded}] - Google Chrome"
        assert decode_framed(title) == json_str

    def test_decode_plain_finds_marker_before_browser_suffix(self):
        title = "tai.sh [holo:cal:abc-123] - Google Chrome"
        assert decode_plain(title) == "cal:abc-123"

    def test_decode_plain_finds_marker_with_em_dash_suffix(self):
        # Firefox uses an em-dash separator
        title = "GitHub [holo:cal:xyz] — Mozilla Firefox"
        assert decode_plain(title) == "cal:xyz"


class TestUtf8RoundTrip:
    """The JS encoder now uses base64-of-UTF-8-bytes (utf8Btoa), so the
    Python decoder must accept Unicode round-tripped that way. macOS
    can sneak Unicode into our path by visually truncating long
    window titles with `…` (U+2026), and host pages can have any
    Unicode in their `originalTitle`.
    """

    def test_decodes_utf8_bytes_emitted_by_js_encoder(self):
        json_str = '{"v":"héllo"}'
        encoded = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
        title = f"page [holo:1:{encoded}]"
        assert decode_framed(title) == json_str

    def test_decodes_unicode_ellipsis(self):
        json_str = '{"session":"a5421…65"}'
        encoded = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
        title = f"page [holo:1:{encoded}]"
        assert decode_framed(title) == json_str
