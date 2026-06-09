"""Tests for `holo.install_remote`.

`subprocess.run` is mocked so no real scp/ssh runs. `_local_jar` is
mocked so the jar download via `ensure_jar()` isn't triggered. Goals:

  - `_local_holo` finds the running binary in frozen and dev modes.
  - `run()` issues scp for both artifacts then ssh with the install
    script piped on stdin.
  - scp / ssh failures propagate as exit code 1.
  - Missing HOST returns exit 2.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from holo import install_remote
from holo.install_remote import InstallRemoteError

# --- _local_holo ----------------------------------------------------------

def test_local_holo_returns_sys_executable_when_frozen(monkeypatch, tmp_path):
    fake = tmp_path / "holo"
    fake.write_text("")
    monkeypatch.setattr(install_remote.sys, "frozen", True, raising=False)
    monkeypatch.setattr(install_remote.sys, "executable", str(fake))
    assert install_remote._local_holo() == fake


def test_local_holo_falls_back_to_which_in_dev_install(monkeypatch):
    monkeypatch.delattr(install_remote.sys, "frozen", raising=False)
    monkeypatch.setattr(
        install_remote.shutil, "which", lambda name: "/opt/anaconda3/bin/holo"
    )
    assert install_remote._local_holo() == Path("/opt/anaconda3/bin/holo")


def test_local_holo_raises_when_no_binary_available(monkeypatch):
    monkeypatch.delattr(install_remote.sys, "frozen", raising=False)
    monkeypatch.setattr(install_remote.shutil, "which", lambda name: None)
    with pytest.raises(InstallRemoteError, match="could not find a `holo`"):
        install_remote._local_holo()


# --- run() happy path ----------------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


@pytest.fixture
def fake_local_artifacts(monkeypatch, tmp_path):
    """Pretend this machine has a holo binary + jar already."""
    holo = tmp_path / "holo"
    holo.write_text("fake binary")
    jar = tmp_path / "sikulixide-2.0.5.jar"
    jar.write_text("fake jar")
    monkeypatch.setattr(install_remote, "_local_holo", lambda: holo)
    monkeypatch.setattr(install_remote, "_local_jar", lambda: jar)
    return holo, jar


def test_run_invokes_scp_then_ssh(fake_local_artifacts, monkeypatch, capsys):
    holo, jar = fake_local_artifacts
    calls: list[dict] = []

    def fake_subprocess_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return _FakeCompleted(0)

    monkeypatch.setattr(install_remote.subprocess, "run", fake_subprocess_run)

    rc = install_remote.run("balexand@machine-b")
    assert rc == 0

    # Expect three calls in order: scp binary, scp jar, ssh install
    assert len(calls) == 3
    assert calls[0]["cmd"] == ["scp", str(holo), "balexand@machine-b:/tmp/holo"]
    assert calls[1]["cmd"] == [
        "scp", str(jar), "balexand@machine-b:/tmp/sikulixide-2.0.5.jar"
    ]
    assert calls[2]["cmd"] == ["ssh", "balexand@machine-b", "bash", "-s"]
    # The install script is piped via stdin.
    script = calls[2]["kwargs"]["input"]
    assert "INSTALL_TARGET=\"$HOME/bin/holo\"" in script
    assert "codesign --force --deep --sign -" in script
    assert "sikulixide-2.0.5.jar" in script
    assert "command -v java" in script


def test_run_accepts_host_without_user_prefix(fake_local_artifacts, monkeypatch, capsys):
    """No user@ prefix → ssh uses the current user; we warn but proceed."""
    monkeypatch.setattr(
        install_remote.subprocess, "run", lambda *a, **kw: _FakeCompleted(0)
    )
    rc = install_remote.run("machine-b")
    assert rc == 0
    assert "no user@ prefix" in capsys.readouterr().err


# --- failures ------------------------------------------------------------

def test_run_returns_2_when_host_empty(capsys):
    rc = install_remote.run("")
    assert rc == 2
    assert "HOST is required" in capsys.readouterr().err


def test_run_returns_1_on_scp_binary_failure(fake_local_artifacts, monkeypatch, capsys):
    def fake_run(cmd, **kw):
        if cmd[0] == "scp":
            return _FakeCompleted(255)  # ssh-style "Connection refused"
        return _FakeCompleted(0)

    monkeypatch.setattr(install_remote.subprocess, "run", fake_run)
    rc = install_remote.run("user@host")
    assert rc == 1
    err = capsys.readouterr().err
    assert "scp failed" in err
    assert "ssh user@host" in err


def test_run_returns_1_when_ssh_script_fails(fake_local_artifacts, monkeypatch, capsys):
    """If the remote install script exits non-zero (e.g. codesign failed
    on B), surface the exit code and return 1."""

    def fake_run(cmd, **kw):
        if cmd[0] == "ssh":
            return _FakeCompleted(7)
        return _FakeCompleted(0)

    monkeypatch.setattr(install_remote.subprocess, "run", fake_run)
    rc = install_remote.run("user@host")
    assert rc == 1
    assert "exited 7" in capsys.readouterr().err


def test_run_returns_1_when_no_local_holo(monkeypatch, capsys):
    monkeypatch.setattr(
        install_remote, "_local_holo",
        lambda: (_ for _ in ()).throw(InstallRemoteError("no binary"))
    )
    rc = install_remote.run("user@host")
    assert rc == 1
    assert "no binary" in capsys.readouterr().err


def test_run_returns_1_when_jar_fetch_fails(monkeypatch, capsys, tmp_path):
    holo = tmp_path / "holo"
    holo.write_text("")
    monkeypatch.setattr(install_remote, "_local_holo", lambda: holo)
    monkeypatch.setattr(
        install_remote, "_local_jar",
        lambda: (_ for _ in ()).throw(RuntimeError("github 503"))
    )
    rc = install_remote.run("user@host")
    assert rc == 1
    err = capsys.readouterr().err
    assert "jar fetch failed" in err
    assert "github 503" in err


# --- CLI dispatch --------------------------------------------------------

def test_cli_install_remote_routes_to_install_remote_module(monkeypatch):
    from holo import cli

    captured: dict = {}

    def fake_run(host):
        captured["host"] = host
        return 0

    monkeypatch.setattr(install_remote, "run", fake_run)
    rc = cli.main(["install-remote", "user@b"])
    assert rc == 0
    assert captured["host"] == "user@b"


def test_cli_install_remote_missing_host_returns_2(capsys):
    from holo import cli

    rc = cli.main(["install-remote"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing HOST" in err
