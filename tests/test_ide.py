"""Tests for `holo ide`.

`subprocess.run` is mocked so the tests don't actually launch a JVM.
`ensure_jar` is mocked to avoid network / disk writes, and
`shutil.which` is patched per-test to simulate java present / absent.
"""

from __future__ import annotations

from holo import cli


def test_ide_refuses_when_java_missing(monkeypatch, capsys):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: None)

    rc = cli._cmd_ide()
    assert rc == 1
    err = capsys.readouterr().err
    assert "`java` not on PATH" in err
    assert "OpenJDK" in err


def test_ide_runs_java_as_subprocess(monkeypatch, tmp_path):
    """Happy path: java on PATH, jar already cached. We must spawn java
    as a CHILD subprocess (not exec) so macOS TCC attributes the IDE's
    mouse / keyboard simulation back to the parent `holo` process,
    inheriting the user's existing Accessibility grant.

    The TCC-correctness invariant we guard:
      - subprocess.run is called with `[java, "-jar", jar]`
      - `cli` module must NOT import `os` (would re-enable execvp path)
    """
    jar = tmp_path / "sikulixide-2.0.5.jar"
    jar.write_bytes(b"fake jar")

    import shutil
    monkeypatch.setattr(shutil, "which", lambda binary: "/opt/homebrew/bin/java")

    from holo import bridge
    monkeypatch.setattr(bridge, "ensure_jar", lambda **kw: jar)

    captured: dict = {}

    class FakeCompleted:
        returncode = 0

    def fake_run(argv, *a, **kw):
        captured["argv"] = list(argv)
        return FakeCompleted()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    # Regression guard: if someone re-introduces `os.execvp` here, they
    # have to re-add `import os` first — fail the test on `import os`
    # alone so we catch the regression on review, not in production.
    assert not hasattr(cli, "os"), (
        "`os` must not be imported in holo.cli — re-adding it implies "
        "someone may have brought back the execvp path, which silently "
        "breaks macOS TCC inheritance for `holo ide`."
    )

    rc = cli._cmd_ide()
    assert rc == 0
    assert captured["argv"] == ["/opt/homebrew/bin/java", "-jar", str(jar)]


def test_ide_returns_java_exit_code(monkeypatch, tmp_path):
    """Whatever `java` exits with propagates back to the shell."""
    jar = tmp_path / "sikulixide-2.0.5.jar"
    jar.write_bytes(b"fake jar")

    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/opt/homebrew/bin/java")
    from holo import bridge
    monkeypatch.setattr(bridge, "ensure_jar", lambda **kw: jar)

    class FakeCompleted:
        returncode = 42

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: FakeCompleted())
    rc = cli._cmd_ide()
    assert rc == 42


def test_ide_propagates_bridge_error(monkeypatch, capsys):
    """If `ensure_jar` raises BridgeMissingError (e.g. download blocked
    via HOLO_BRIDGE_NO_DOWNLOAD=1), surface a clean message and don't
    spawn the JVM."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/opt/homebrew/bin/java")

    from holo import bridge

    def boom(**kw):
        raise bridge.BridgeMissingError("no jar and downloads disabled")

    monkeypatch.setattr(bridge, "ensure_jar", boom)

    called: dict = {"run": False}
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **kw: called.__setitem__("run", True),
    )

    rc = cli._cmd_ide()
    assert rc == 1
    assert called["run"] is False
    err = capsys.readouterr().err
    assert "no jar and downloads disabled" in err
