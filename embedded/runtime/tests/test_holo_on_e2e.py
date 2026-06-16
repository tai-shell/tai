"""End-to-end smoke for Phase 3.A dispatch.

Spawns a real ``holo mcp --announce --resources-config FILE`` as a
background subprocess, waits for the mDNS announce to settle, then
runs ``holo-on`` against it and asserts on stdout + exit code.

Skipped automatically when the ``holo`` CLI isn't on PATH (or
``HOLO_CLI`` isn't set) — the test depends on a real holo install and
on multicast working in the test environment. CI runners without
mDNS will hit the skip path.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _find_holo_cli() -> str | None:
    """Resolve the holo CLI path.

    Order: ``$HOLO_CLI`` env override, then ``shutil.which("holo")``.
    Returns None if neither resolves — the caller skips the test.
    """
    override = os.environ.get("HOLO_CLI")
    if override and Path(override).is_file():
        return override
    return shutil.which("holo")


HOLO_CLI = _find_holo_cli()
SKIP_REASON = (
    "holo CLI not found on PATH and HOLO_CLI not set — set HOLO_CLI=/path/"
    "to/holo to run the e2e smoke locally"
)


@pytest.mark.skipif(HOLO_CLI is None, reason=SKIP_REASON)
class TestHoloOnEndToEnd:
    @pytest.fixture
    def daemon_with_resource(self, tmp_path: Path):
        """Spawn ``holo mcp --listen --announce --resources-config FILE``.

        Yields ``(holo_cli, resource_path, cfg_path)``. The fixture
        terminates the daemon on teardown and waits up to 5s for the
        announce to propagate before yielding so the discovery
        snapshot has something to find.
        """
        assert HOLO_CLI is not None
        resource_dir = tmp_path / "resource"
        resource_dir.mkdir()
        # Decoy mp4 files for find / wc to operate on.
        for n in ("a.mp4", "b.mp4", "c.txt"):
            (resource_dir / n).touch()
        cfg = tmp_path / "resources.toml"
        cfg.write_text(
            f"""
[resources.movies]
path = "{resource_dir}"
tags = ["video-files"]
caps = ["exec:find", "exec:wc"]
"""
        )
        # Use a high port unlikely to clash in CI.
        proc = subprocess.Popen(
            [
                HOLO_CLI,
                "mcp",
                "--listen", "17779",
                "--announce",
                "--resources-config", str(cfg),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give mDNS time to settle — the announce + browser handshake
        # is comfortably under 5s on every machine I've seen.
        time.sleep(5)
        try:
            yield HOLO_CLI, resource_dir, cfg
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def test_dispatch_runs_body_on_matching_daemon(
        self, daemon_with_resource
    ) -> None:
        holo_cli, _resource_dir, cfg = daemon_with_resource
        from tai_runtime.holo_on.dispatch import dispatch

        out, err = io.StringIO(), io.StringIO()
        rc = dispatch(
            selector_str="[holo:tag=video-files:5m:settle=3000ms]",
            body='find . -name "*.mp4" | wc -l',
            holo_command=holo_cli,
            holo_extra_args=["--resources-config", str(cfg)],
            stdout=out,
            stderr=err,
        )
        assert rc == 0, (
            f"dispatch exit={rc} stderr={err.getvalue()[:300]!r}"
        )
        # The body produces `       2` on stdout (wc -l of two .mp4 files).
        assert "2" in out.getvalue(), (
            f"expected '2' in stdout, got {out.getvalue()!r}"
        )

    def test_disallowed_body_returns_nonzero(
        self, daemon_with_resource
    ) -> None:
        holo_cli, _resource_dir, cfg = daemon_with_resource
        from tai_runtime.holo_on.dispatch import dispatch

        out, err = io.StringIO(), io.StringIO()
        rc = dispatch(
            selector_str="[holo:tag=video-files:5m:settle=3000ms]",
            body="rm -rf .",  # not in caps
            holo_command=holo_cli,
            holo_extra_args=["--resources-config", str(cfg)],
            stdout=out,
            stderr=err,
        )
        assert rc != 0
        assert "body-rejected" in err.getvalue()

    def test_unknown_tag_zero_matches_exit_0(
        self, daemon_with_resource
    ) -> None:
        holo_cli, _resource_dir, cfg = daemon_with_resource
        from tai_runtime.holo_on.dispatch import dispatch

        out, err = io.StringIO(), io.StringIO()
        rc = dispatch(
            selector_str="[holo:tag=does-not-exist:settle=2000ms]",
            body='echo hi',
            holo_command=holo_cli,
            holo_extra_args=["--resources-config", str(cfg)],
            stdout=out,
            stderr=err,
        )
        # Broadcast + no matches → exit 0 (nothing to do is not an error).
        assert rc == 0
        assert "no daemons matched" in err.getvalue()

    def test_bad_selector_exits_2(
        self, daemon_with_resource
    ) -> None:
        # No need for the daemon for this test, but using the fixture
        # to share the CLI lookup.
        holo_cli, _, cfg = daemon_with_resource
        from tai_runtime.holo_on.dispatch import dispatch

        out, err = io.StringIO(), io.StringIO()
        rc = dispatch(
            selector_str="[totally invalid]",
            body="echo hi",
            holo_command=holo_cli,
            holo_extra_args=["--resources-config", str(cfg)],
            stdout=out,
            stderr=err,
        )
        assert rc == 2
        assert "holo:" in err.getvalue()


@pytest.mark.skipif(
    HOLO_CLI is None, reason=SKIP_REASON
)
def test_python_m_holo_on_help_runs() -> None:
    """``python -m tai_runtime.holo_on --help`` exits 0 with usage text.

    Catches packaging regressions (broken __main__.py, etc.) without
    needing a live daemon.
    """
    here = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tai_runtime.holo_on",
            "--help",
        ],
        env={**os.environ, "PYTHONPATH": str(here)},
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: holo-on" in result.stdout
