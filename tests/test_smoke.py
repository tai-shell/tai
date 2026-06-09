import holo
from holo.cli import main


def test_version_attr():
    assert isinstance(holo.__version__, str)
    assert holo.__version__ != ""


def test_cli_version_flag(capsys):
    rc = main(["--version"])
    assert rc == 0
    captured = capsys.readouterr()
    assert holo.__version__ in captured.out


def test_cli_default(capsys):
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "holo" in captured.out


def test_cli_help_flag(capsys):
    for flag in ["--help", "-h", "help"]:
        rc = main([flag])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Usage:" in out
        # A few subcommands must show up so the help is actually useful.
        for sub in ("doctor", "demo", "mcp", "install-bookmarklet"):
            assert sub in out
        # Renamed surfaces should appear under their new names; the old
        # names should not — keeps the help honest.
        for sub in ("screen", "install-screen", "--no-bookmarklet"):
            assert sub in out
        assert "install-bridge" not in out


def test_cli_unknown_command_points_at_help(capsys):
    rc = main(["nopenope"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown command" in err
    assert "--help" in err


def test_cli_old_bridge_subcommand_is_unknown(capsys):
    """The `holo bridge` subcommand was renamed to `holo screen`. Hitting
    the old name should fall through to the unknown-command handler so
    users get a pointer instead of silent confusion."""
    rc = main(["bridge", "ping"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown command" in err
