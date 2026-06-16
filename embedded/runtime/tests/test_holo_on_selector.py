"""Unit tests for the selector parser.

Covers the grammar from ``tai_runtime.holo_on.selector`` end-to-end:
broadcast vs single-target, predicates (tag/name/host/resource),
suffix order independence (timeout, settle, mode), and the full
catalogue of structural failures.
"""

from __future__ import annotations

import pytest

from tai_runtime.holo_on.selector import (
    Selector,
    SelectorError,
    parse_selector,
)


class TestParseSelectorHappyPath:
    @pytest.mark.parametrize(
        "spec,expected",
        [
            (
                "[holo:tag=video-files]",
                Selector(broadcast=True, predicates={"tag": "video-files"}),
            ),
            (
                "[holo:tag=video-files:5m]",
                Selector(
                    broadcast=True,
                    predicates={"tag": "video-files"},
                    timeout_s=300.0,
                ),
            ),
            (
                "{holo:host=nas-01:30s}",
                Selector(
                    broadcast=False,
                    predicates={"host": "nas-01"},
                    timeout_s=30.0,
                ),
            ),
            (
                "[holo:tag=video-files,name=movies:5m:tagged]",
                Selector(
                    broadcast=True,
                    predicates={"tag": "video-files", "name": "movies"},
                    timeout_s=300.0,
                    mode="tagged",
                ),
            ),
            (
                "[holo:tag=a:5m:settle=500ms:tagged]",
                Selector(
                    broadcast=True,
                    predicates={"tag": "a"},
                    timeout_s=300.0,
                    settle_ms=500,
                    mode="tagged",
                ),
            ),
            (
                "{holo:host=x:json}",
                Selector(
                    broadcast=False,
                    predicates={"host": "x"},
                    mode="json",
                ),
            ),
            (
                "[holo:5m]",
                Selector(broadcast=True, timeout_s=300.0),
            ),
            (
                "[holo:1h]",
                Selector(broadcast=True, timeout_s=3600.0),
            ),
            (
                "[holo:tag=a:tagged:5m:settle=200ms]",
                # Suffix order is free
                Selector(
                    broadcast=True,
                    predicates={"tag": "a"},
                    timeout_s=300.0,
                    settle_ms=200,
                    mode="tagged",
                ),
            ),
            (
                "[holo:resource=movies]",
                Selector(
                    broadcast=True, predicates={"resource": "movies"}
                ),
            ),
            (
                "[holo:settle=2s]",
                Selector(broadcast=True, settle_ms=2000),
            ),
        ],
    )
    def test_parses_as_expected(
        self, spec: str, expected: Selector
    ) -> None:
        assert parse_selector(spec) == expected

    def test_whitespace_stripped(self) -> None:
        assert parse_selector("  [holo:tag=a]  ") == parse_selector(
            "[holo:tag=a]"
        )


class TestParseSelectorErrors:
    @pytest.mark.parametrize(
        "spec,error_fragment",
        [
            ("", "empty"),
            ("[]", "'holo:'"),
            ("[tag=foo]", "'holo:'"),
            ("[holo:tag=a", "wrapped"),
            ("[holo:tag=a}", "wrapped"),
            ("[holo:bogus=1]", "unknown key"),
            ("[holo:tag=]", "empty value"),
            ("[holo:tag=a,tag=b]", "specified twice"),
            ("[holo:5m:tag=a]", "predicates must precede"),
            ("[holo:tag=a:5m:30s]", "timeout specified twice"),
            ("[holo:tag=a:tagged:json]", "mode specified twice"),
            ("[holo:tag=a:0m]", "must be > 0"),
            ("[holo:tag=a:settle=5min]", "bad settle"),
            ("[holo:tag=a:woof]", "not a recognised"),
            ("[holo:tag=a,]", "empty predicate entry"),
            ("[holo:tag=a:settle=10ms:settle=20ms]", "'settle' specified twice"),
        ],
    )
    def test_rejects(self, spec: str, error_fragment: str) -> None:
        with pytest.raises(SelectorError, match=error_fragment):
            parse_selector(spec)
