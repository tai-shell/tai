"""Tests for `holo.upgrade`.

All network I/O is mocked. The goal is to verify version parsing,
asset/install-path selection, cache behaviour, and the upgrade flow's
ability to atomically replace a fake-running binary.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from holo import upgrade

# --- parse_version --------------------------------------------------------

@pytest.mark.parametrize(
    "raw,ok",
    [
        ("v0.1.0a23", True),
        ("0.1.0a23", True),
        ("v0.1.0", True),
        ("v0.1.0rc1", True),
        ("v0.1.0b2", True),
        ("v1.2.3.post4", True),
        ("vendored-sikulix-2.0.5", False),
        ("v0.1", False),
        ("", False),
        ("v0.1.0dev1", False),
    ],
)
def test_parse_version_recognises_known_shapes(raw, ok):
    parsed = upgrade.parse_version(raw)
    if ok:
        assert parsed is not None
    else:
        assert parsed is None


def test_version_ordering():
    # alpha < beta < rc < release < post
    assert upgrade.parse_version("v0.1.0a1") < upgrade.parse_version("v0.1.0a2")
    assert upgrade.parse_version("v0.1.0a99") < upgrade.parse_version("v0.1.0b1")
    assert upgrade.parse_version("v0.1.0b1") < upgrade.parse_version("v0.1.0rc1")
    assert upgrade.parse_version("v0.1.0rc1") < upgrade.parse_version("v0.1.0")
    assert upgrade.parse_version("v0.1.0") < upgrade.parse_version("v0.1.0.post1")
    assert upgrade.parse_version("v0.1.0") < upgrade.parse_version("v0.2.0")
    assert upgrade.parse_version("v0.1.0a23") == upgrade.parse_version("0.1.0a23")


# --- asset_name / install_path -------------------------------------------

def test_asset_name_macos(monkeypatch):
    monkeypatch.setattr(upgrade.sys, "platform", "darwin")
    assert upgrade.asset_name() == "holo-macos-universal2"


def test_asset_name_linux(monkeypatch):
    monkeypatch.setattr(upgrade.sys, "platform", "linux")
    monkeypatch.setattr(upgrade.platform, "machine", lambda: "x86_64")
    assert upgrade.asset_name() == "holo-linux-x86_64"


def test_asset_name_linux_arm_refuses(monkeypatch):
    monkeypatch.setattr(upgrade.sys, "platform", "linux")
    monkeypatch.setattr(upgrade.platform, "machine", lambda: "aarch64")
    with pytest.raises(upgrade.UpgradeError, match="linux/aarch64"):
        upgrade.asset_name()


def test_asset_name_windows(monkeypatch):
    monkeypatch.setattr(upgrade.sys, "platform", "win32")
    assert upgrade.asset_name() == "holo-windows-x86_64.exe"


def test_install_path_refuses_dev_install(monkeypatch):
    monkeypatch.delattr(upgrade.sys, "frozen", raising=False)
    with pytest.raises(upgrade.UpgradeError, match="dev install"):
        upgrade.install_path()


def test_install_path_when_frozen(monkeypatch, tmp_path):
    fake_bin = tmp_path / "holo"
    fake_bin.write_text("")
    monkeypatch.setattr(upgrade.sys, "frozen", True, raising=False)
    monkeypatch.setattr(upgrade.sys, "executable", str(fake_bin))
    assert upgrade.install_path() == fake_bin


# --- fetch_latest_tag -----------------------------------------------------

class _FakeResponse:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _releases_payload(*tags: str) -> bytes:
    return json.dumps([{"tag_name": t} for t in tags]).encode()


def test_fetch_latest_tag_picks_highest_v_tag(monkeypatch):
    payload = _releases_payload(
        "vendored-sikulix-2.0.5",
        "v0.1.0a23",
        "v0.1.0a24",
        "v0.1.0a22",
        "not-a-version",
    )
    monkeypatch.setattr(
        upgrade.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse(payload),
    )
    assert upgrade.fetch_latest_tag(timeout=1.0) == "v0.1.0a24"


def test_fetch_latest_tag_raises_on_no_version_tags(monkeypatch):
    payload = _releases_payload("vendored-sikulix-2.0.5", "not-a-version")
    monkeypatch.setattr(
        upgrade.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse(payload),
    )
    with pytest.raises(upgrade.UpgradeError, match="no holo version tags"):
        upgrade.fetch_latest_tag(timeout=1.0)


def test_fetch_latest_tag_http_error(monkeypatch):
    def boom(*a, **kw):
        raise urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
    monkeypatch.setattr(upgrade.urllib.request, "urlopen", boom)
    with pytest.raises(upgrade.UpgradeError, match="HTTP 503"):
        upgrade.fetch_latest_tag(timeout=1.0)


def test_fetch_latest_tag_url_error(monkeypatch):
    def boom(*a, **kw):
        raise urllib.error.URLError("dns down")
    monkeypatch.setattr(upgrade.urllib.request, "urlopen", boom)
    with pytest.raises(upgrade.UpgradeError, match="dns down"):
        upgrade.fetch_latest_tag(timeout=1.0)


# --- check_for_update / cache --------------------------------------------

@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, "cache_dir", lambda: tmp_path)
    return tmp_path


def test_check_for_update_returns_newer_tag(isolated_cache, monkeypatch):
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a23")
    monkeypatch.setattr(
        upgrade, "fetch_latest_tag", lambda **kw: "v0.1.0a24"
    )
    assert upgrade.check_for_update() == "v0.1.0a24"
    # Cache was written.
    cached = json.loads((isolated_cache / "version_check.json").read_text())
    assert cached["tag"] == "v0.1.0a24"


def test_check_for_update_returns_none_when_at_latest(isolated_cache, monkeypatch):
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a24")
    monkeypatch.setattr(
        upgrade, "fetch_latest_tag", lambda **kw: "v0.1.0a24"
    )
    assert upgrade.check_for_update() is None


def test_check_for_update_uses_fresh_cache(isolated_cache, monkeypatch):
    cache_file = isolated_cache / "version_check.json"
    cache_file.write_text(
        json.dumps({"checked_at": time.time(), "tag": "v0.1.0a99"})
    )
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a23")

    def fail_fetch(**kw):
        raise AssertionError("should not hit network when cache is fresh")

    monkeypatch.setattr(upgrade, "fetch_latest_tag", fail_fetch)
    assert upgrade.check_for_update() == "v0.1.0a99"


def test_check_for_update_ignores_stale_cache(isolated_cache, monkeypatch):
    cache_file = isolated_cache / "version_check.json"
    cache_file.write_text(
        json.dumps({
            "checked_at": time.time() - upgrade.CACHE_TTL_SECONDS - 1,
            "tag": "v0.0.0a1",
        })
    )
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a23")
    monkeypatch.setattr(
        upgrade, "fetch_latest_tag", lambda **kw: "v0.1.0a24"
    )
    assert upgrade.check_for_update() == "v0.1.0a24"


def test_check_for_update_silent_on_network_error(isolated_cache, monkeypatch):
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a23")

    def boom(**kw):
        raise upgrade.UpgradeError("network down")

    monkeypatch.setattr(upgrade, "fetch_latest_tag", boom)
    assert upgrade.check_for_update() is None


# --- run_upgrade ----------------------------------------------------------

@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    """Pretend holo is installed at tmp_path/holo on macOS."""
    fake_bin = tmp_path / "holo"
    fake_bin.write_bytes(b"old binary contents")
    fake_bin.chmod(0o755)

    monkeypatch.setattr(upgrade.sys, "platform", "darwin")
    monkeypatch.setattr(upgrade.sys, "frozen", True, raising=False)
    monkeypatch.setattr(upgrade.sys, "executable", str(fake_bin))
    # Suppress codesign by routing through subprocess.run mock.
    monkeypatch.setattr(
        upgrade.subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0})()
    )
    return fake_bin


def test_run_upgrade_replaces_binary_in_place(fake_install, monkeypatch, isolated_cache, capsys):
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a23")
    monkeypatch.setattr(upgrade, "fetch_latest_tag", lambda **kw: "v0.1.0a24")
    new_bytes = b"NEW BINARY CONTENTS"

    captured: dict = {}

    def fake_urlopen(url, **kw):
        captured["url"] = url
        return _FakeResponse(new_bytes)

    monkeypatch.setattr(upgrade.urllib.request, "urlopen", fake_urlopen)

    rc = upgrade.run_upgrade()
    assert rc == 0
    assert fake_install.read_bytes() == new_bytes
    assert "holo-macos-universal2" in captured["url"]
    assert "v0.1.0a24" in captured["url"]
    out = capsys.readouterr().out
    assert "upgraded to v0.1.0a24" in out


def test_run_upgrade_noop_when_already_latest(fake_install, monkeypatch, isolated_cache, capsys):
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a24")
    monkeypatch.setattr(upgrade, "fetch_latest_tag", lambda **kw: "v0.1.0a24")
    # urlopen must NOT be called when we're already at latest.
    monkeypatch.setattr(
        upgrade.urllib.request,
        "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no download expected")),
    )
    rc = upgrade.run_upgrade()
    assert rc == 0
    assert "already up to date" in capsys.readouterr().out


def test_run_upgrade_force_redownloads(fake_install, monkeypatch, isolated_cache):
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a24")
    monkeypatch.setattr(upgrade, "fetch_latest_tag", lambda **kw: "v0.1.0a24")
    monkeypatch.setattr(
        upgrade.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse(b"forced replacement"),
    )
    rc = upgrade.run_upgrade(force=True)
    assert rc == 0
    assert fake_install.read_bytes() == b"forced replacement"


def test_run_upgrade_refuses_dev_install(monkeypatch, capsys):
    monkeypatch.delattr(upgrade.sys, "frozen", raising=False)
    rc = upgrade.run_upgrade()
    assert rc == 1
    err = capsys.readouterr().err
    assert "dev install" in err


def test_run_upgrade_handles_download_failure(fake_install, monkeypatch, isolated_cache, capsys):
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a23")
    monkeypatch.setattr(upgrade, "fetch_latest_tag", lambda **kw: "v0.1.0a24")

    def boom(*a, **kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(upgrade.urllib.request, "urlopen", boom)
    rc = upgrade.run_upgrade()
    assert rc == 1
    assert "connection refused" in capsys.readouterr().err
    # Old binary still in place.
    assert fake_install.read_bytes() == b"old binary contents"
    # No leftover .holo-upgrade-* in install dir.
    leftovers = list(fake_install.parent.glob(".holo-upgrade-*"))
    assert leftovers == [], leftovers


def test_run_upgrade_clears_cache_after_install(fake_install, monkeypatch, isolated_cache):
    cache_file = isolated_cache / "version_check.json"
    cache_file.write_text(
        json.dumps({"checked_at": time.time(), "tag": "v0.1.0a24"})
    )
    monkeypatch.setattr(upgrade, "__version__", "0.1.0a23")
    monkeypatch.setattr(upgrade, "fetch_latest_tag", lambda **kw: "v0.1.0a24")
    monkeypatch.setattr(
        upgrade.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse(b"x"),
    )
    rc = upgrade.run_upgrade()
    assert rc == 0
    assert not cache_file.exists()
