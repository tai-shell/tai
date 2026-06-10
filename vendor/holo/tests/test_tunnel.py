"""Unit tests for holo.tunnel.

Pin: ssh argv construction, IP selection from CloudCity records,
stderr parsing for the "Allocated port N" line, lifecycle
(start/stop/idempotency), and the failure modes (timeout, ssh
exiting before announcing a port).

We don't actually spawn ssh — the real-network behaviour is covered
by the manual smoke test in the spec. The mocked subprocess.Popen
gives us exact control over what the "ssh process" emits on stderr
and when, which is what we need to pin the contract.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from holo import cloudcity_announce as cc_announce
from holo import tunnel as tunnel_mod

# --------------------------------------------------------- argv construction


def test_ssh_argv_includes_required_flags(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    argv = tunnel_mod._build_ssh_argv(
        target_ip="192.168.1.5",
        target_port=2222,
        key_path=key,
        principal="lando",
        local_port=0,
    )
    assert argv[0] == "ssh"
    assert "-N" in argv  # no remote command
    assert "-A" in argv  # agent forward
    assert "-i" in argv and argv[argv.index("-i") + 1] == str(key)
    assert "-R" in argv and argv[argv.index("-R") + 1] == "0:localhost:22"
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"
    assert argv[-1] == "lando@192.168.1.5"
    # Hardening options that prevent prompts / forward failures.
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined
    assert "ExitOnForwardFailure=yes" in joined
    assert "IdentitiesOnly=yes" in joined
    assert "ServerAliveInterval=30" in joined


def test_ssh_argv_custom_local_port_and_principal(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    argv = tunnel_mod._build_ssh_argv(
        target_ip="10.0.0.1",
        target_port=22,
        key_path=key,
        principal="alice",
        local_port=12345,
    )
    assert "-R" in argv and argv[argv.index("-R") + 1] == "12345:localhost:22"
    assert argv[-1] == "alice@10.0.0.1"


def test_ssh_argv_appends_extra_args_before_target(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    argv = tunnel_mod._build_ssh_argv(
        target_ip="10.0.0.1",
        target_port=22,
        key_path=key,
        principal="lando",
        local_port=0,
        extra_ssh_args=["-vvv"],
    )
    assert "-vvv" in argv
    assert argv[-1] == "lando@10.0.0.1"


# ------------------------------------------------------------ IP selection


def test_pick_ip_returns_first_announced() -> None:
    record = {
        "instance": "cc-x",
        cc_announce.FIELD_IPS: ["10.55.195.6", "192.168.1.5"],
        cc_announce.FIELD_HOST: "MacBook.local",
    }
    assert tunnel_mod._pick_ip(record) == "10.55.195.6"


def test_pick_ip_falls_back_to_host_when_no_ips() -> None:
    record = {
        "instance": "cc-x",
        cc_announce.FIELD_HOST: "MacBook.local",
    }
    assert tunnel_mod._pick_ip(record) == "MacBook.local"


def test_pick_ip_raises_when_neither_present() -> None:
    record = {"instance": "cc-x"}
    with pytest.raises(tunnel_mod.TunnelError, match="no ips or host"):
        tunnel_mod._pick_ip(record)


# ------------------------------------------------------------------- env


def test_parse_principal_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOLO_TUNNEL_PRINCIPAL", raising=False)
    assert tunnel_mod.parse_principal_from_env() == "lando"


def test_parse_principal_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOLO_TUNNEL_PRINCIPAL", "operator")
    assert tunnel_mod.parse_principal_from_env() == "operator"


# ----------------------------------------------------------- Tunnel.start()


def _make_proc_mock(
    stderr_lines: list[bytes],
    *,
    stay_alive: bool = True,
    exit_code: int = 0,
) -> MagicMock:
    """Build a MagicMock for subprocess.Popen with a controllable stderr.

    ``stderr_lines`` are the bytes-with-newline lines the fake "ssh"
    will emit, in order, before stderr "closes" (returns b""). If
    ``stay_alive`` is True, ``poll()`` keeps returning None until
    the test calls ``proc._terminate_now()`` — which makes ``wait()``
    return immediately and ``poll()`` return ``exit_code``.
    """
    proc = MagicMock()
    proc.stderr = io.BytesIO(b"".join(stderr_lines))
    # If we start dead, the exit_code is already set so poll() returns it
    # immediately. If we start alive, exit_code stays None until wait()
    # or terminate()/kill() flips state["alive"] to False.
    initial_exit: int | None = exit_code if not stay_alive else None
    state: dict[str, Any] = {"alive": stay_alive, "exit_code": initial_exit}

    def poll() -> int | None:
        return None if state["alive"] else state["exit_code"]

    def wait(timeout: float | None = None) -> int:
        del timeout
        state["alive"] = False
        if state["exit_code"] is None:
            state["exit_code"] = exit_code
        return state["exit_code"]

    def terminate_now(code: int = 0) -> None:
        state["alive"] = False
        state["exit_code"] = code

    def terminate() -> None:
        terminate_now(143)  # SIGTERM convention

    def kill() -> None:
        terminate_now(137)  # SIGKILL convention

    proc.poll.side_effect = poll
    proc.wait.side_effect = wait
    proc.terminate.side_effect = terminate
    proc.kill.side_effect = kill
    proc.returncode = None
    proc._terminate_now = terminate_now  # test-only handle
    return proc


def test_start_returns_announced_port(tmp_path: Path) -> None:
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_HOST: "test.local",
        cc_announce.FIELD_PORT: 2222,
    }
    proc = _make_proc_mock(
        [
            b"some-noise: connecting...\n",
            b"Allocated port 51492 for remote forward to localhost:22\n",
        ]
    )
    with patch(
        "holo.tunnel.subprocess.Popen", return_value=proc
    ) as popen_mock:
        t = tunnel_mod.Tunnel(
            cloudcity_record=record, key_path=tmp_path / "host-key"
        )
        port = t.start(timeout=5.0)
    assert port == 51492
    assert t.port == 51492
    assert t.target == ("192.168.1.5", 2222)
    # Verify Popen got something resembling an ssh argv.
    argv = popen_mock.call_args[0][0]
    assert argv[0] == "ssh"
    assert argv[-1] == f"{tunnel_mod.DEFAULT_PRINCIPAL}@192.168.1.5"
    t.stop()


def test_start_idempotent(tmp_path: Path) -> None:
    """Calling start() twice doesn't spawn a second ssh."""
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    proc = _make_proc_mock(
        [b"Allocated port 9001 for remote forward to localhost:22\n"]
    )
    with patch(
        "holo.tunnel.subprocess.Popen", return_value=proc
    ) as popen_mock:
        t = tunnel_mod.Tunnel(
            cloudcity_record=record, key_path=tmp_path / "host-key"
        )
        t.start(timeout=5.0)
        # Second call must short-circuit without another Popen.
        port = t.start(timeout=5.0)
    assert port == 9001
    popen_mock.assert_called_once()
    t.stop()


def test_start_raises_when_ssh_exits_early(tmp_path: Path) -> None:
    """ssh dies before announcing a port → TunnelExited."""
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    # No "Allocated port" line; process exits with code 255 (auth failure).
    proc = _make_proc_mock(
        [b"Permission denied (publickey).\n"],
        stay_alive=False,
        exit_code=255,
    )
    proc.returncode = 255
    with patch("holo.tunnel.subprocess.Popen", return_value=proc):
        t = tunnel_mod.Tunnel(
            cloudcity_record=record, key_path=tmp_path / "host-key"
        )
        with pytest.raises(tunnel_mod.TunnelExited, match="exited with code 255"):
            t.start(timeout=2.0)


def test_start_raises_on_timeout(tmp_path: Path) -> None:
    """ssh stays alive but never announces a port → TunnelStartTimeout."""
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    # Empty stderr; process stays alive (poll returns None forever).
    proc = _make_proc_mock([], stay_alive=True)
    with patch("holo.tunnel.subprocess.Popen", return_value=proc):
        t = tunnel_mod.Tunnel(
            cloudcity_record=record, key_path=tmp_path / "host-key"
        )
        with pytest.raises(tunnel_mod.TunnelStartTimeout):
            t.start(timeout=0.3)
    # The process should have been terminated on the way out.
    proc.terminate.assert_called()


def test_start_uses_announced_port(tmp_path: Path) -> None:
    """Target sshd port comes from the CloudCity record's `port` field."""
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2223,
    }
    proc = _make_proc_mock(
        [b"Allocated port 9001 for remote forward to localhost:22\n"]
    )
    with patch(
        "holo.tunnel.subprocess.Popen", return_value=proc
    ) as popen_mock:
        t = tunnel_mod.Tunnel(
            cloudcity_record=record, key_path=tmp_path / "host-key"
        )
        t.start(timeout=5.0)
    argv = popen_mock.call_args[0][0]
    p_index = argv.index("-p")
    assert argv[p_index + 1] == "2223"
    t.stop()


# ------------------------------------------------------------------ stop()


def test_stop_terminates_running_proc(tmp_path: Path) -> None:
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    proc = _make_proc_mock(
        [b"Allocated port 9001 for remote forward to localhost:22\n"]
    )
    with patch("holo.tunnel.subprocess.Popen", return_value=proc):
        t = tunnel_mod.Tunnel(
            cloudcity_record=record, key_path=tmp_path / "host-key"
        )
        t.start(timeout=5.0)
        t.stop()
    proc.terminate.assert_called_once()


def test_stop_idempotent(tmp_path: Path) -> None:
    """stop() before start() and double-stop are both no-ops."""
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    t = tunnel_mod.Tunnel(
        cloudcity_record=record, key_path=tmp_path / "host-key"
    )
    t.stop()  # before start — no-op

    proc = _make_proc_mock(
        [b"Allocated port 9001 for remote forward to localhost:22\n"]
    )
    with patch("holo.tunnel.subprocess.Popen", return_value=proc):
        t.start(timeout=5.0)
        t.stop()
        t.stop()  # second stop — no-op
    proc.terminate.assert_called_once()


# ----------------------------------------------------- open_to_cloudcity()


def test_open_to_cloudcity_refreshes_cert_then_starts(tmp_path: Path) -> None:
    """Convenience wrapper calls cert.get_or_refresh before starting."""
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    proc = _make_proc_mock(
        [b"Allocated port 9001 for remote forward to localhost:22\n"]
    )
    with patch(
        "holo.cert.get_or_refresh", return_value={"has_cert": True}
    ) as cert_mock, patch(
        "holo.tunnel.subprocess.Popen", return_value=proc
    ):
        t = tunnel_mod.open_to_cloudcity(
            record,
            backend="http://localhost:8081",
            key_path=tmp_path / "host-key",
        )
    assert t.port == 9001
    cert_mock.assert_called_once()
    # cert_force_refresh defaults to False
    _, kwargs = cert_mock.call_args
    assert kwargs.get("force") is False
    t.stop()


def test_open_to_cloudcity_force_refresh_propagates(tmp_path: Path) -> None:
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    proc = _make_proc_mock(
        [b"Allocated port 9001 for remote forward to localhost:22\n"]
    )
    with patch(
        "holo.cert.get_or_refresh", return_value={"has_cert": True}
    ) as cert_mock, patch(
        "holo.tunnel.subprocess.Popen", return_value=proc
    ):
        t = tunnel_mod.open_to_cloudcity(
            record,
            backend="http://localhost:8081",
            key_path=tmp_path / "host-key",
            cert_force_refresh=True,
        )
    _, kwargs = cert_mock.call_args
    assert kwargs.get("force") is True
    t.stop()


# ------------------------------------------------------------- find_cloudcity


def test_find_cloudcity_matches_by_instance() -> None:
    fake_zc = MagicMock()
    fake_browser = MagicMock()

    class FakeStore:
        def snapshot(self) -> list[dict[str, Any]]:
            return [
                {
                    "instance": "cc-bravo",
                    cc_announce.FIELD_HOST: "bravo.local",
                },
                {
                    "instance": "cc-alpha",
                    cc_announce.FIELD_HOST: "alpha.local",
                },
            ]

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(fake_zc, fake_browser, FakeStore()),
    ):
        record = tunnel_mod.find_cloudcity("cc-alpha", wait_s=0.2)
    assert record is not None
    assert record["instance"] == "cc-alpha"


def test_find_cloudcity_matches_by_host_fallback() -> None:
    fake_zc = MagicMock()
    fake_browser = MagicMock()

    class FakeStore:
        def snapshot(self) -> list[dict[str, Any]]:
            return [
                {
                    "instance": "cc-alpha-7f2e1b",
                    cc_announce.FIELD_HOST: "MacBook-Air.local",
                }
            ]

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(fake_zc, fake_browser, FakeStore()),
    ):
        record = tunnel_mod.find_cloudcity("MacBook-Air.local", wait_s=0.2)
    assert record is not None
    assert record["instance"] == "cc-alpha-7f2e1b"


def test_find_cloudcity_returns_none_when_missing() -> None:
    fake_zc = MagicMock()
    fake_browser = MagicMock()

    class FakeStore:
        def snapshot(self) -> list[dict[str, Any]]:
            return []

    with patch(
        "holo.cloudcity_discover._start_browser",
        return_value=(fake_zc, fake_browser, FakeStore()),
    ):
        record = tunnel_mod.find_cloudcity("does-not-exist", wait_s=0.2)
    assert record is None
    fake_zc.close.assert_called_once()


# ============================================================================
# HoloMCPServer.holo_tunnel_up / holo_tunnel_down — Phase 4b integration
# ============================================================================
#
# Skipped automatically when the `mcp` package isn't installed (the
# entire mcp_server module fails to import). CI installs it; the local
# dev env intentionally doesn't to keep iteration on non-MCP code fast.


@pytest.fixture
def _holo_mcp_server_or_skip() -> Any:
    """Import HoloMCPServer or skip the test if mcp isn't available."""
    try:
        from holo.mcp_server import HoloMCPServer
    except ImportError:
        pytest.skip("mcp package not installed in this env")
    return HoloMCPServer


def _make_fake_tunnel(port: int = 51492) -> MagicMock:
    """Tunnel double for the MCP integration tests."""
    t = MagicMock()
    t.port = port
    t.target = ("192.168.1.5", 2222)
    return t


def test_holo_tunnel_up_returns_port_and_metadata(
    _holo_mcp_server_or_skip: Any,
) -> None:
    HoloMCPServer = _holo_mcp_server_or_skip

    record = {
        "instance": "cc-test",
        cc_announce.FIELD_HOST: "MacBook.local",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    fake_tunnel = _make_fake_tunnel(port=51492)
    with patch(
        "holo.tunnel.find_cloudcity", return_value=record
    ), patch(
        "holo.tunnel.open_to_cloudcity", return_value=fake_tunnel
    ):
        server = HoloMCPServer(no_bookmarklet=True)
        try:
            result = server.holo_tunnel_up("cc-test")
        finally:
            server.shutdown()

    assert result["tunnel_port"] == 51492
    assert result["cloudcity_instance"] == "cc-test"
    assert result["cloudcity_host"] == "MacBook.local"
    assert result["cloudcity_target"] == "192.168.1.5:2222"


def test_holo_tunnel_up_unknown_instance_raises(
    _holo_mcp_server_or_skip: Any,
) -> None:
    HoloMCPServer = _holo_mcp_server_or_skip
    with patch("holo.tunnel.find_cloudcity", return_value=None):
        server = HoloMCPServer(no_bookmarklet=True)
        try:
            with pytest.raises(RuntimeError, match="no CloudCity matching"):
                server.holo_tunnel_up("does-not-exist")
        finally:
            server.shutdown()


def test_holo_tunnel_up_replaces_existing_tunnel(
    _holo_mcp_server_or_skip: Any,
) -> None:
    """A second up() stops the prior tunnel before opening a new one."""
    HoloMCPServer = _holo_mcp_server_or_skip
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_HOST: "MacBook.local",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    first = _make_fake_tunnel(port=51000)
    second = _make_fake_tunnel(port=51001)
    with patch(
        "holo.tunnel.find_cloudcity", return_value=record
    ), patch(
        "holo.tunnel.open_to_cloudcity", side_effect=[first, second]
    ):
        server = HoloMCPServer(no_bookmarklet=True)
        try:
            server.holo_tunnel_up("cc-test")
            server.holo_tunnel_up("cc-test")
        finally:
            server.shutdown()

    first.stop.assert_called_once()


def test_holo_tunnel_up_updates_announcer_tunnel_port(
    _holo_mcp_server_or_skip: Any,
) -> None:
    """When --announce is on, the tunnel_port flows into the mDNS record."""
    HoloMCPServer = _holo_mcp_server_or_skip
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_HOST: "MacBook.local",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    fake_tunnel = _make_fake_tunnel(port=42424)
    with patch("holo.tunnel.find_cloudcity", return_value=record), patch(
        "holo.tunnel.open_to_cloudcity", return_value=fake_tunnel
    ), patch("holo.announce.HoloAnnouncer") as ann_cls:
        server = HoloMCPServer(no_bookmarklet=True, announce=True)
        try:
            server.holo_tunnel_up("cc-test")
        finally:
            server.shutdown()
    ann_cls.return_value.set_tunnel_port.assert_any_call(42424)


def test_holo_tunnel_down_stops_tunnel_and_clears_announce(
    _holo_mcp_server_or_skip: Any,
) -> None:
    HoloMCPServer = _holo_mcp_server_or_skip
    record = {
        "instance": "cc-test",
        cc_announce.FIELD_HOST: "MacBook.local",
        cc_announce.FIELD_IPS: ["192.168.1.5"],
        cc_announce.FIELD_PORT: 2222,
    }
    fake_tunnel = _make_fake_tunnel(port=42424)
    with patch("holo.tunnel.find_cloudcity", return_value=record), patch(
        "holo.tunnel.open_to_cloudcity", return_value=fake_tunnel
    ), patch("holo.announce.HoloAnnouncer") as ann_cls:
        server = HoloMCPServer(no_bookmarklet=True, announce=True)
        try:
            server.holo_tunnel_up("cc-test")
            result = server.holo_tunnel_down()
        finally:
            server.shutdown()

    assert result == {"closed": True}
    fake_tunnel.stop.assert_called()
    ann_cls.return_value.set_tunnel_port.assert_any_call(None)


def test_holo_tunnel_down_with_no_active_tunnel_returns_closed_false(
    _holo_mcp_server_or_skip: Any,
) -> None:
    HoloMCPServer = _holo_mcp_server_or_skip
    server = HoloMCPServer(no_bookmarklet=True)
    try:
        result = server.holo_tunnel_down()
    finally:
        server.shutdown()
    assert result == {"closed": False}
