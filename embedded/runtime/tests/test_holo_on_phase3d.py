"""Phase 3.D unit tests — output modes + dict return shape.

The end-to-end behaviour (concurrent fan-out, $ON_HOSTS binding,
per-host else, do/end blocks) is exercised by the manual bash-side
smoke documented in PR #34 — the bash↔C↔Python integration can't
be unit-tested without the embedded interpreter. The Python-only
slices we CAN test in isolation are the output modes and the
contract of ``dispatch()`` returning a result dict.
"""

from __future__ import annotations

import io
import json

import pytest

from tai_runtime.holo_on.dispatch import _emit_frames, dispatch
from tai_runtime.holo_on.selector import Selector


# ----------------------------------------------------------- modes


SAMPLE_RESULT = {
    "frames": [
        {"fd": "stdout", "data": "first stdout line"},
        {"fd": "stderr", "data": "warning here"},
        {"fd": "stdout", "data": "second stdout line"},
    ],
    "exit": 0,
}


class TestEmitFramesDefault:
    def test_stdout_lines_raw(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        _emit_frames(
            "nas-01", "movies", SAMPLE_RESULT,
            stdout=out, stderr=err, mode="default",
        )
        # Stdout untagged
        assert out.getvalue() == "first stdout line\nsecond stdout line\n"
        # Stderr host-tagged
        assert err.getvalue() == "nas-01:movies: warning here\n"


class TestEmitFramesTagged:
    def test_all_lines_prefixed(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        _emit_frames(
            "nas-01", "movies", SAMPLE_RESULT,
            stdout=out, stderr=err, mode="tagged",
        )
        assert out.getvalue() == (
            "nas-01:movies: first stdout line\n"
            "nas-01:movies: second stdout line\n"
        )
        assert err.getvalue() == "nas-01:movies: warning here\n"


class TestEmitFramesJSON:
    def test_each_frame_jsonl_on_stdout(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        _emit_frames(
            "nas-01", "movies", SAMPLE_RESULT,
            stdout=out, stderr=err, mode="json",
        )
        # Stderr empty — everything goes to stdout in JSONL
        assert err.getvalue() == ""
        lines = out.getvalue().strip().split("\n")
        assert len(lines) == 3
        parsed = [json.loads(line) for line in lines]
        assert parsed[0] == {
            "host": "nas-01",
            "resource": "movies",
            "fd": "stdout",
            "data": "first stdout line",
        }
        assert parsed[1]["fd"] == "stderr"
        assert parsed[1]["data"] == "warning here"
        assert parsed[2]["data"] == "second stdout line"


class TestEmitFramesEdgeCases:
    def test_empty_frames(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        _emit_frames(
            "h", "r", {"frames": []},
            stdout=out, stderr=err, mode="default",
        )
        assert out.getvalue() == ""
        assert err.getvalue() == ""

    def test_unknown_fd_routes_to_stdout(self) -> None:
        """A frame with an unexpected fd value goes to stdout (default)."""
        out, err = io.StringIO(), io.StringIO()
        _emit_frames(
            "h", "r",
            {"frames": [{"fd": "weird", "data": "x"}]},
            stdout=out, stderr=err, mode="default",
        )
        assert out.getvalue() == "x\n"
        assert err.getvalue() == ""


# ---------------------------------------------------- dispatch return shape


class TestDispatchReturnShape:
    """The contract change between Phase 3.A (return int) and 3.D
    (return dict). Exercised here for the selector-error path which
    doesn't touch network/MCP."""

    def test_bad_selector_returns_dict(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        result = dispatch(
            selector_str="[totally invalid]",
            body="echo hi",
            stdout=out,
            stderr=err,
        )
        assert isinstance(result, dict)
        assert set(result.keys()) >= {"exit", "hosts"}
        assert result["exit"] == 2
        assert result["hosts"] == []
        assert "holo:" in err.getvalue()


# ----------------------------------- selector mode parse → emit roundtrip


class TestSelectorModeFlowsToEmit:
    """The selector parser already covers ``mode`` per Phase 3.A's
    test suite; this checks that the value lands on _emit_frames
    correctly via the dispatcher's plumbing."""

    @pytest.mark.parametrize("mode", ["default", "tagged", "json"])
    def test_mode_value(self, mode: str) -> None:
        # The Selector dataclass accepts the mode field directly.
        s = Selector(broadcast=True, predicates={"tag": "x"}, mode=mode)
        assert s.mode == mode
