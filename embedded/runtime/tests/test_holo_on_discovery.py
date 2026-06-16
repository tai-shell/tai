"""Unit tests for the discovery filter logic.

The live mDNS browse is exercised by the end-to-end smoke
(:mod:`test_holo_on_e2e`). Here we only test the predicate matcher,
which takes a session dict (the parse_txt output) and a Selector and
returns bool.
"""

from __future__ import annotations

from holo.announce import FIELD_HOST, FIELD_R, FIELD_RN
from tai_runtime.holo_on.discovery import _matches
from tai_runtime.holo_on.selector import Selector


SAMPLE = {
    FIELD_HOST: "nas-01",
    FIELD_R: ["video-files", "archive"],
    FIELD_RN: ["movies", "family"],
}


class TestMatches:
    def test_no_predicates_matches_anything(self) -> None:
        assert _matches(SAMPLE, Selector(broadcast=True)) is True

    def test_tag_match(self) -> None:
        assert _matches(
            SAMPLE,
            Selector(broadcast=True, predicates={"tag": "video-files"}),
        )

    def test_tag_miss(self) -> None:
        assert not _matches(
            SAMPLE, Selector(broadcast=True, predicates={"tag": "photos"})
        )

    def test_tag_and_required(self) -> None:
        assert _matches(
            SAMPLE,
            Selector(
                broadcast=True,
                predicates={"tag": "video-files", "host": "nas-01"},
            ),
        )
        assert not _matches(
            SAMPLE,
            Selector(
                broadcast=True,
                predicates={"tag": "video-files", "host": "other"},
            ),
        )

    def test_name_match(self) -> None:
        assert _matches(
            SAMPLE, Selector(broadcast=True, predicates={"name": "movies"})
        )

    def test_resource_alias_for_name(self) -> None:
        # `resource=X` matches against the rn= field, same as `name=X`.
        # The dispatch layer re-filters against the full resource list
        # for the final cross-check.
        assert _matches(
            SAMPLE,
            Selector(broadcast=True, predicates={"resource": "movies"}),
        )

    def test_host_predicate(self) -> None:
        assert _matches(
            SAMPLE, Selector(broadcast=False, predicates={"host": "nas-01"})
        )
        assert not _matches(
            SAMPLE,
            Selector(broadcast=False, predicates={"host": "other"}),
        )

    def test_empty_session_fails_non_empty_filters(self) -> None:
        empty: dict = {}
        assert _matches(empty, Selector(broadcast=True))  # no preds → ok
        assert not _matches(
            empty, Selector(broadcast=True, predicates={"tag": "x"})
        )
        assert not _matches(
            empty, Selector(broadcast=True, predicates={"host": "x"})
        )
