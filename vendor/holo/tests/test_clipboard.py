"""Tests for holo.clipboard.

The module wraps `pyperclip` and `pyautogui` for clipboard read/write
plus the Cmd/Ctrl+V keystroke. We can't deterministically exercise
real OS clipboard / keystroke behavior in CI (no real focused window),
so the tests mock both libraries and verify the call sequence and
contents.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from holo import clipboard


@pytest.fixture
def fake_clip():
    """Mock pyperclip with an in-memory clipboard string."""
    state = {"value": "PRIOR"}

    def _copy(text):
        state["value"] = text

    def _paste():
        return state["value"]

    with (
        patch("pyperclip.copy", side_effect=_copy) as copy_mock,
        patch("pyperclip.paste", side_effect=_paste) as paste_mock,
    ):
        yield {"copy": copy_mock, "paste": paste_mock, "state": state}


@pytest.fixture
def fake_hotkey():
    with patch("pyautogui.hotkey") as m:
        yield m


def test_read_returns_clipboard_value(fake_clip):
    fake_clip["state"]["value"] = "hello"
    assert clipboard.read() == "hello"


def test_write_replaces_clipboard(fake_clip):
    clipboard.write("new contents")
    assert fake_clip["state"]["value"] == "new contents"


def test_paste_writes_then_keystroke_then_restores(fake_clip, fake_hotkey):
    fake_clip["state"]["value"] = "USER_DATA"
    # Track call order across both mocks via a side-effect log.
    calls: list[str] = []
    fake_clip["copy"].side_effect = lambda t: (
        calls.append(f"copy:{t}"),
        fake_clip["state"].update(value=t),
    )
    fake_hotkey.side_effect = lambda *a, **k: calls.append(f"hotkey:{a}")

    clipboard.paste("PAYLOAD", settle_seconds=0, paste_seconds=0)

    # Order: write payload → send hotkey → restore prior contents
    assert calls[0] == "copy:PAYLOAD"
    assert calls[1].startswith("hotkey:")
    assert calls[2] == "copy:USER_DATA"


def test_paste_skips_restore_when_disabled(fake_clip, fake_hotkey):
    fake_clip["state"]["value"] = "USER_DATA"
    clipboard.paste("PAYLOAD", restore=False, settle_seconds=0, paste_seconds=0)
    assert fake_clip["state"]["value"] == "PAYLOAD"
    # Only one copy call — no restore.
    copy_calls = [c.args[0] for c in fake_clip["copy"].call_args_list]
    assert copy_calls == ["PAYLOAD"]


def test_paste_uses_command_modifier_on_darwin(fake_clip, fake_hotkey):
    with patch.object(clipboard.sys, "platform", "darwin"):
        clipboard.paste("X", restore=False, settle_seconds=0, paste_seconds=0)
    fake_hotkey.assert_called_once_with("command", "v")


def test_paste_uses_ctrl_modifier_off_darwin(fake_clip, fake_hotkey):
    with patch.object(clipboard.sys, "platform", "linux"):
        clipboard.paste("X", restore=False, settle_seconds=0, paste_seconds=0)
    fake_hotkey.assert_called_once_with("ctrl", "v")


def test_paste_sleeps_for_configured_durations(fake_clip, fake_hotkey):
    sleeps: list[float] = []
    with patch.object(clipboard.time, "sleep", side_effect=sleeps.append):
        clipboard.paste("X", settle_seconds=0.2, paste_seconds=0.3)
    assert sleeps == [0.2, 0.3]


def test_paste_zero_durations_skips_sleeps(fake_clip, fake_hotkey):
    sleeps: list[float] = []
    with patch.object(clipboard.time, "sleep", side_effect=sleeps.append):
        clipboard.paste("X", settle_seconds=0, paste_seconds=0)
    assert sleeps == []
