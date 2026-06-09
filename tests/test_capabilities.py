"""Unit tests for holo.capabilities and holo.capabilities_server.

The probe layer is auto: every supported package manager is run when
its binary is on PATH; the curated `--probe-software` whitelist is
gone. We mock the subprocess calls because the test box doesn't have
all of (brew, apt, port, pacman, snap, flatpak, winget, choco, scoop,
pip, pipx, cargo, npm, gem, conda) installed at once.

For applications: filesystem walk + mdfind, both mocked.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

from holo.capabilities import (
    CAPABILITIES_SCHEMA_VERSION,
    SUPPORTED_PKG_MANAGERS,
    CapabilitiesProbe,
    probe_applications,
    probe_hardware,
    probe_packages,
)


def _stub_run(stdout: str, returncode: int = 0) -> Any:
    """Build a CompletedProcess-like stub for subprocess.run."""

    class _R:
        def __init__(self, stdout: str, returncode: int) -> None:
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = ""

    return _R(stdout, returncode)


# ============================================================================
# Schema + aggregator
# ============================================================================


class TestSchema:
    def test_schema_version_is_2(self) -> None:
        assert CAPABILITIES_SCHEMA_VERSION == 2

    def test_response_has_no_software_field(self) -> None:
        # Schema 2 dropped the curated software field entirely.
        # Applications come through the platform-native catalog;
        # everything else through packages.*.
        with (
            patch("holo.capabilities.shutil.which", return_value=None),
            patch("holo.capabilities.platform.system", return_value="Linux"),
        ):
            snap = CapabilitiesProbe().collect()
        assert "software" not in snap
        assert set(snap.keys()) >= {
            "schema",
            "host",
            "applications",
            "packages",
            "generated_at",
        }


# ============================================================================
# Hardware
# ============================================================================


class TestProbeHardware:
    def test_returns_expected_keys(self) -> None:
        info = probe_hardware()
        for key in ("os", "os_version", "arch", "cpu_model", "cores", "ram_gb"):
            assert key in info, f"missing {key} in {info}"

    def test_cores_positive(self) -> None:
        assert probe_hardware()["cores"] >= 1

    def test_os_lowercase(self) -> None:
        info = probe_hardware()
        assert info["os"] == info["os"].lower()
        assert info["os"] in {"darwin", "linux", "windows"}


# ============================================================================
# Applications
# ============================================================================


class TestProbeApplicationsLinux:
    def test_returns_empty_on_linux(self) -> None:
        # Linux apps come through packages.* (apt/dnf/snap/flatpak),
        # not as a separate catalog. The applications dict stays empty.
        with patch(
            "holo.capabilities.platform.system", return_value="Linux"
        ):
            assert probe_applications() == {}


class TestProbeApplicationsMacOS:
    def _fake_scandir(
        self, by_dir: dict[str, list[str]]
    ):
        """Return a `scandir` replacement that yields fake DirEntry-likes
        per directory mapping."""

        class _Entry:
            def __init__(self, name: str, path: str) -> None:
                self.name = name
                self.path = path

            def is_dir(self, follow_symlinks: bool = True) -> bool:
                return self.name.endswith(".app")

        class _Iter:
            def __init__(self, items: list[_Entry]) -> None:
                self._items = items

            def __iter__(self):
                return iter(self._items)

            def __enter__(self):
                return self

            def __exit__(self, *args: Any) -> None:
                pass

            def close(self) -> None:
                pass

        def fake(d: str):
            entries = by_dir.get(d)
            if entries is None:
                raise OSError(f"no such dir: {d}")
            return _Iter(
                [
                    _Entry(name, f"{d.rstrip('/')}/{name}")
                    for name in entries
                ]
            )

        return fake

    def test_walks_standard_dirs(self) -> None:
        scandir = self._fake_scandir(
            {
                "/Applications": [
                    "Google Chrome.app",
                    "Firefox.app",
                    "README.txt",  # not an .app — skipped
                ],
                "/Applications/Utilities": ["Terminal.app"],
                "/System/Applications": ["Safari.app"],
            }
        )
        with (
            patch(
                "holo.capabilities.platform.system", return_value="Darwin"
            ),
            patch("holo.capabilities.os.scandir", side_effect=scandir),
            # Stub mdfind to return nothing — we're testing the walk only.
            patch(
                "holo.capabilities._run_pkg_command", return_value=""
            ),
        ):
            apps = probe_applications()

        assert "Google Chrome" in apps
        assert "Firefox" in apps
        assert "Terminal" in apps
        assert "Safari" in apps
        assert "README" not in apps
        assert apps["Firefox"]["path"] == "/Applications/Firefox.app"

    def test_mdfind_fills_in_outliers(self) -> None:
        scandir = self._fake_scandir(
            {
                "/Applications": ["Boring.app"],
            }
        )
        # mdfind reports an app outside the standard dirs.
        mdfind_output = (
            "/Applications/Boring.app\n"
            "/Users/alice/Dev/MyApp.app\n"
            "/System/Library/PrivateFrameworks/Helper.app\n"
            "\n"
        )
        with (
            patch(
                "holo.capabilities.platform.system", return_value="Darwin"
            ),
            patch("holo.capabilities.os.scandir", side_effect=scandir),
            patch(
                "holo.capabilities._run_pkg_command",
                return_value=mdfind_output,
            ),
        ):
            apps = probe_applications()

        assert "Boring" in apps
        # mdfind found a dev build outside the standard dirs.
        assert "MyApp" in apps
        assert apps["MyApp"]["path"] == "/Users/alice/Dev/MyApp.app"
        # Private system helpers under /System/Library/ are filtered.
        assert "Helper" not in apps

    def test_mdfind_does_not_overwrite_walk_results(self) -> None:
        # If mdfind reports the same .app at a different path, the
        # walk's canonical path wins (stable preference for /Applications).
        scandir = self._fake_scandir(
            {"/Applications": ["Chrome.app"]}
        )
        mdfind_output = "/Users/alice/Apps/Chrome.app\n"
        with (
            patch(
                "holo.capabilities.platform.system", return_value="Darwin"
            ),
            patch("holo.capabilities.os.scandir", side_effect=scandir),
            patch(
                "holo.capabilities._run_pkg_command",
                return_value=mdfind_output,
            ),
        ):
            apps = probe_applications()
        assert apps["Chrome"]["path"] == "/Applications/Chrome.app"

    def test_walk_picks_up_version_and_bundle_id_from_real_plist(
        self, tmp_path: Any
    ) -> None:
        """End-to-end metadata read against actual `plistlib`-written
        Info.plists in a tmp dir."""
        import plistlib

        apps_dir = tmp_path / "Applications"
        apps_dir.mkdir()

        def _make_app(name: str, plist: dict[str, str]) -> None:
            bundle = apps_dir / f"{name}.app"
            (bundle / "Contents").mkdir(parents=True)
            with open(bundle / "Contents" / "Info.plist", "wb") as f:
                plistlib.dump(plist, f)

        _make_app(
            "FakeChrome",
            {
                "CFBundleShortVersionString": "147.0.7727.138",
                "CFBundleIdentifier": "com.fake.Chrome",
            },
        )
        _make_app(
            "FakeBare",
            {},  # no version, no bundle_id
        )
        _make_app(
            "FakeBuildOnly",
            {"CFBundleVersion": "42"},  # falls back to build version
        )

        # Need a real scandir against the tmp_path for this test, so
        # don't patch os.scandir. Just patch _MACOS_APP_DIRS to point
        # at the tmp dir, and platform.system + _run_pkg_command.
        with (
            patch(
                "holo.capabilities.platform.system", return_value="Darwin"
            ),
            patch(
                "holo.capabilities._MACOS_APP_DIRS",
                (str(apps_dir),),
            ),
            patch(
                "holo.capabilities._run_pkg_command", return_value=""
            ),
        ):
            apps = probe_applications()

        assert apps["FakeChrome"]["version"] == "147.0.7727.138"
        assert apps["FakeChrome"]["bundle_id"] == "com.fake.Chrome"
        # Bare app — only `path` populated.
        assert "version" not in apps["FakeBare"]
        assert "bundle_id" not in apps["FakeBare"]
        # Falls back to CFBundleVersion when CFBundleShortVersionString is missing.
        assert apps["FakeBuildOnly"]["version"] == "42"

    def test_unreadable_plist_falls_back_to_path_only(
        self, tmp_path: Any
    ) -> None:
        # Create a .app whose Info.plist is corrupt — the entry should
        # still appear in the result with just `path`.
        apps_dir = tmp_path / "Applications"
        apps_dir.mkdir()
        bundle = apps_dir / "Corrupt.app"
        (bundle / "Contents").mkdir(parents=True)
        (bundle / "Contents" / "Info.plist").write_bytes(b"not a plist")

        with (
            patch(
                "holo.capabilities.platform.system", return_value="Darwin"
            ),
            patch(
                "holo.capabilities._MACOS_APP_DIRS", (str(apps_dir),)
            ),
            patch(
                "holo.capabilities._run_pkg_command", return_value=""
            ),
        ):
            apps = probe_applications()

        assert "Corrupt" in apps
        assert apps["Corrupt"] == {"path": str(bundle)}

    def test_old_style_ascii_plist_falls_back_to_path_only(
        self, tmp_path: Any
    ) -> None:
        """Regression: NeXTSTEP-style ASCII plists raise
        `xml.parsers.expat.ExpatError` from `plistlib.load`. Earlier
        versions only caught `OSError` / `InvalidFileException` /
        `ValueError`, so the exception bubbled up the entire ASGI
        stack and crashed the /capabilities request with HTTP 500
        (which discover.sh reports as `unreachable` because of
        `curl --fail`). Apple still ships some bundles in this format.
        """
        apps_dir = tmp_path / "Applications"
        apps_dir.mkdir()
        bundle = apps_dir / "AsciiPlist.app"
        (bundle / "Contents").mkdir(parents=True)
        (bundle / "Contents" / "Info.plist").write_bytes(
            b"{\n"
            b"    CFBundleName = \"AsciiPlist\";\n"
            b"    CFBundleVersion = \"1.0\";\n"
            b"    CFBundleIdentifier = \"com.example.AsciiPlist\";\n"
            b"}\n"
        )

        with (
            patch(
                "holo.capabilities.platform.system", return_value="Darwin"
            ),
            patch(
                "holo.capabilities._MACOS_APP_DIRS", (str(apps_dir),)
            ),
            patch(
                "holo.capabilities._run_pkg_command", return_value=""
            ),
        ):
            apps = probe_applications()

        # Entry still appears — just no metadata. The probe MUST NOT
        # raise, because that crashes the whole capabilities sweep.
        assert "AsciiPlist" in apps
        assert apps["AsciiPlist"] == {"path": str(bundle)}

    def test_skips_system_library_paths(self) -> None:
        from holo.capabilities import _is_system_app_bundle

        assert _is_system_app_bundle(
            "/System/Library/PrivateFrameworks/Foo.app"
        )
        assert _is_system_app_bundle(
            "/Library/Application Support/Bar.app"
        )
        assert _is_system_app_bundle(
            "/usr/libexec/Quux.app"
        )
        assert not _is_system_app_bundle(
            "/Applications/Google Chrome.app"
        )
        assert not _is_system_app_bundle(
            "/System/Applications/Safari.app"
        )


class TestProbeApplicationsWindows:
    """`winreg` only exists on Windows; we inject a minimal fake module
    via sys.modules so the function-internal `import winreg` line picks
    it up. Verifies the field extraction list (including the new
    install_date addition)."""

    def test_install_date_extracted(self, monkeypatch: Any) -> None:
        import sys
        from unittest.mock import MagicMock

        # The values for the one app we'll surface.
        per_app_values = {
            "DisplayName": ("MyTestApp", 1),
            "DisplayVersion": ("1.2.3", 1),
            "Publisher": ("Acme Corp", 1),
            "InstallLocation": (r"C:\Program Files\MyTestApp", 1),
            "InstallDate": ("20251102", 1),
        }

        # Track per-root state so EnumKey only returns subkeys for one root.
        state: dict[str, Any] = {"subkey_count": 0}

        def open_key(hive_or_handle: Any, subkey: str | None = None):
            handle = MagicMock()
            if subkey is not None:
                # Top-level open — only the plain HKLM\...\Uninstall
                # (not WOW6432Node, not HKCU) gets a non-empty list.
                if (
                    hive_or_handle == 0  # HKLM
                    and "WOW6432Node" not in subkey
                ):
                    state["subkey_count"] = 1
                else:
                    state["subkey_count"] = 0
            return handle

        def enum_key(root_handle: Any, i: int) -> str:
            if i < state["subkey_count"]:
                return "MyAppRegEntry"
            raise OSError("end of enumeration")

        def query_value_ex(sub_handle: Any, src: str) -> tuple[Any, int]:
            if src in per_app_values:
                return per_app_values[src]
            raise OSError(f"value missing: {src}")

        wr = MagicMock()
        wr.HKEY_LOCAL_MACHINE = 0
        wr.HKEY_CURRENT_USER = 1
        wr.OpenKey = open_key
        wr.EnumKey = enum_key
        wr.QueryValueEx = query_value_ex

        monkeypatch.setitem(sys.modules, "winreg", wr)

        from holo.capabilities import _probe_applications_windows

        out = _probe_applications_windows()

        assert "MyTestApp" in out
        e = out["MyTestApp"]
        assert e["version"] == "1.2.3"
        assert e["publisher"] == "Acme Corp"
        assert e["path"] == r"C:\Program Files\MyTestApp"
        assert e["install_date"] == "20251102"

    def test_install_date_omitted_when_absent(
        self, monkeypatch: Any
    ) -> None:
        # InstallDate is registry-optional — many entries don't have
        # it. Verify we omit the key rather than emit "" or null.
        import sys
        from unittest.mock import MagicMock

        per_app_values = {
            "DisplayName": ("AppNoDate", 1),
            "DisplayVersion": ("0.9.0", 1),
            # No InstallDate, no Publisher, no InstallLocation.
        }
        state: dict[str, Any] = {"subkey_count": 0}

        def open_key(hive_or_handle: Any, subkey: str | None = None):
            handle = MagicMock()
            if subkey is not None:
                state["subkey_count"] = (
                    1
                    if hive_or_handle == 0 and "WOW6432Node" not in subkey
                    else 0
                )
            return handle

        def enum_key(root_handle: Any, i: int) -> str:
            if i < state["subkey_count"]:
                return "AppNoDateRegEntry"
            raise OSError("end")

        def query_value_ex(sub_handle: Any, src: str) -> tuple[Any, int]:
            if src in per_app_values:
                return per_app_values[src]
            raise OSError(f"value missing: {src}")

        wr = MagicMock()
        wr.HKEY_LOCAL_MACHINE = 0
        wr.HKEY_CURRENT_USER = 1
        wr.OpenKey = open_key
        wr.EnumKey = enum_key
        wr.QueryValueEx = query_value_ex

        monkeypatch.setitem(sys.modules, "winreg", wr)

        from holo.capabilities import _probe_applications_windows

        out = _probe_applications_windows()

        assert "AppNoDate" in out
        e = out["AppNoDate"]
        assert "install_date" not in e
        assert "publisher" not in e
        # `path` always present (defaults to "" when InstallLocation absent).
        assert e["path"] == ""


# ============================================================================
# Packages — auto-run, no opt-in
# ============================================================================


class TestProbePackagesAuto:
    def test_only_installed_managers_appear(self) -> None:
        # Only `brew` is on PATH; only `brew` should land in the result.
        def fake_which(name: str) -> str | None:
            return "/x/brew" if name == "brew" else None

        with (
            patch("holo.capabilities.shutil.which", side_effect=fake_which),
            patch(
                "holo.capabilities.subprocess.run",
                return_value=_stub_run("ffmpeg 7.0.1\n"),
            ),
        ):
            out = probe_packages()
        assert set(out.keys()) == {"brew"}
        assert out["brew"] == [{"name": "ffmpeg", "version": "7.0.1"}]

    def test_aliases_collapse_to_canonical_key(self) -> None:
        # `dpkg` aliases to `apt`. Even if `dpkg-query` is on PATH, the
        # output key is `apt`, not `dpkg`.
        def fake_which(name: str) -> str | None:
            return "/x/dpkg-query" if name == "dpkg-query" else None

        with (
            patch("holo.capabilities.shutil.which", side_effect=fake_which),
            patch(
                "holo.capabilities.subprocess.run",
                return_value=_stub_run("bash 5.2\n"),
            ),
        ):
            out = probe_packages()
        assert "apt" in out
        assert "dpkg" not in out
        assert "dnf" not in out  # dnf is an alias for rpm; rpm not on PATH

    def test_dnf_yum_share_rpm_probe(self) -> None:
        # When `rpm` is on PATH, the output key is `rpm`. We don't
        # double-emit dnf and yum.
        def fake_which(name: str) -> str | None:
            return "/x/rpm" if name == "rpm" else None

        with (
            patch("holo.capabilities.shutil.which", side_effect=fake_which),
            patch(
                "holo.capabilities.subprocess.run",
                return_value=_stub_run("bash 5.2\n"),
            ),
        ):
            out = probe_packages()
        assert "rpm" in out
        assert "dnf" not in out
        assert "yum" not in out


class TestPackageManagerCoverage:
    def test_supported_list_matches_dispatch(self) -> None:
        from holo.capabilities import _PKG_PROBES

        assert set(SUPPORTED_PKG_MANAGERS) == set(_PKG_PROBES.keys())

    def test_supported_includes_language_level(self) -> None:
        # Routing whisper installs by `pip` was a key motivation.
        for name in ("pip", "pipx", "cargo", "npm", "gem", "conda"):
            assert name in SUPPORTED_PKG_MANAGERS

    def test_supported_includes_linux_third_party(self) -> None:
        for name in ("snap", "flatpak"):
            assert name in SUPPORTED_PKG_MANAGERS

    def test_supported_includes_windows(self) -> None:
        for name in ("winget", "choco", "scoop"):
            assert name in SUPPORTED_PKG_MANAGERS


# ============================================================================
# Per-probe parsers (mocked subprocess)
# ============================================================================


def _patched_probe(probe_name: str, stdout: str):
    """Helper: patch shutil.which → present, subprocess.run → stdout."""
    return [
        patch(
            "holo.capabilities.shutil.which",
            return_value=f"/x/{probe_name}",
        ),
        patch(
            "holo.capabilities.subprocess.run",
            return_value=_stub_run(stdout),
        ),
    ]


class TestParsers:
    def _run(self, mgr: str, stdout: str) -> list[dict[str, str]]:
        from holo.capabilities import _PKG_PROBES

        with (
            patch(
                "holo.capabilities.shutil.which",
                return_value=f"/x/{mgr}",
            ),
            patch(
                "holo.capabilities.subprocess.run",
                return_value=_stub_run(stdout),
            ),
        ):
            return _PKG_PROBES[mgr]() or []

    def test_brew_multiple_versions_takes_first(self) -> None:
        out = self._run("brew", "openssl@3 3.4.1 3.4.0\n")
        assert out == [{"name": "openssl@3", "version": "3.4.1"}]

    def test_apt_via_dpkg_query(self) -> None:
        out = self._run("apt", "bash 5.2.21-2\nzsh 5.9-6\n")
        assert out == [
            {"name": "bash", "version": "5.2.21-2"},
            {"name": "zsh", "version": "5.9-6"},
        ]

    def test_port(self) -> None:
        sample = (
            "The following ports are currently installed:\n"
            "  ffmpeg @7.0.1_0 (active)\n"
            "  aom @3.12.1_0 (active)\n"
        )
        out = self._run("port", sample)
        assert out == [
            {"name": "ffmpeg", "version": "7.0.1"},
            {"name": "aom", "version": "3.12.1"},
        ]

    def test_pacman(self) -> None:
        out = self._run("pacman", "bash 5.2.21-1\nzsh 5.9-1\n")
        assert out == [
            {"name": "bash", "version": "5.2.21-1"},
            {"name": "zsh", "version": "5.9-1"},
        ]

    def test_snap(self) -> None:
        sample = (
            "Name        Version    Rev    Tracking       Publisher    Notes\n"
            "core24      2.18       2354   latest/stable  canonical    base\n"
            "firefox     125.0.3    4173   latest/stable  mozilla      -\n"
        )
        out = self._run("snap", sample)
        assert out == [
            {"name": "core24", "version": "2.18"},
            {"name": "firefox", "version": "125.0.3"},
        ]

    def test_flatpak(self) -> None:
        # Tab-separated when --columns=application,version is set.
        out = self._run(
            "flatpak",
            "org.gnome.Calculator\t50.1\nio.github.thetumultuousunicornofdarkness.css-grid-generator\t0.5.2\n",
        )
        assert out == [
            {"name": "org.gnome.Calculator", "version": "50.1"},
            {
                "name": "io.github.thetumultuousunicornofdarkness.css-grid-generator",
                "version": "0.5.2",
            },
        ]

    def test_winget(self) -> None:
        sample = (
            "Name      Id              Version\n"
            "----------------------------------\n"
            "Git       Git.Git         2.45.0\n"
            "Firefox   Mozilla.Firefox 125.0\n"
        )
        out = self._run("winget", sample)
        assert out == [
            {"name": "Git", "version": "2.45.0"},
            {"name": "Firefox", "version": "125.0"},
        ]

    def test_choco(self) -> None:
        out = self._run("choco", "git|2.45.0\nffmpeg|7.0.1\n")
        assert out == [
            {"name": "git", "version": "2.45.0"},
            {"name": "ffmpeg", "version": "7.0.1"},
        ]

    def test_scoop(self) -> None:
        sample = (
            "Name        Version    Source\n"
            "------------------------------\n"
            "git         2.45.0     main\n"
            "ffmpeg      7.0.1      main\n"
        )
        out = self._run("scoop", sample)
        assert out == [
            {"name": "git", "version": "2.45.0"},
            {"name": "ffmpeg", "version": "7.0.1"},
        ]

    def test_pip_freeze(self) -> None:
        out = self._run(
            "pip",
            "requests==2.31.0\npyyaml==6.0.1\nsome-editable @ file:///x\n",
        )
        assert out == [
            {"name": "requests", "version": "2.31.0"},
            {"name": "pyyaml", "version": "6.0.1"},
            {"name": "some-editable", "version": ""},
        ]

    def test_pipx(self) -> None:
        out = self._run("pipx", "ruff 0.6.0\nblack 24.4.2\n")
        assert out == [
            {"name": "ruff", "version": "0.6.0"},
            {"name": "black", "version": "24.4.2"},
        ]

    def test_cargo(self) -> None:
        sample = (
            "ripgrep v14.1.0:\n"
            "    rg\n"
            "fd-find v9.0.0:\n"
            "    fd\n"
        )
        out = self._run("cargo", sample)
        assert out == [
            {"name": "ripgrep", "version": "14.1.0"},
            {"name": "fd-find", "version": "9.0.0"},
        ]

    def test_npm_global_json(self) -> None:
        sample = (
            '{"dependencies":{"typescript":{"version":"5.4.5"},'
            '"@anthropic-ai/claude-code":{"version":"0.5.1"}}}'
        )
        out = self._run("npm", sample)
        # Order may vary depending on dict ordering in JSON parse;
        # match by set rather than list equality.
        assert {(p["name"], p["version"]) for p in out} == {
            ("typescript", "5.4.5"),
            ("@anthropic-ai/claude-code", "0.5.1"),
        }

    def test_gem(self) -> None:
        sample = (
            "*** LOCAL GEMS ***\n"
            "\n"
            "rake (13.0.6, 13.0.3)\n"
            "bundler (2.5.5)\n"
        )
        out = self._run("gem", sample)
        # The header line has no `(`, gets skipped.
        assert {(p["name"], p["version"]) for p in out} == {
            ("rake", "13.0.6"),
            ("bundler", "2.5.5"),
        }

    def test_conda_json(self) -> None:
        sample = (
            "[{\"name\":\"numpy\",\"version\":\"1.26.4\"},"
            "{\"name\":\"scipy\",\"version\":\"1.13.0\"}]"
        )
        out = self._run("conda", sample)
        assert {(p["name"], p["version"]) for p in out} == {
            ("numpy", "1.26.4"),
            ("scipy", "1.13.0"),
        }


class TestProbeFailures:
    def test_command_failure_omits_entry(self) -> None:
        with (
            patch(
                "holo.capabilities.shutil.which", return_value="/x/brew"
            ),
            patch(
                "holo.capabilities.subprocess.run",
                return_value=_stub_run("", returncode=1),
            ),
        ):
            out = probe_packages()
        assert "brew" not in out

    def test_subprocess_raises_omits_entry(self) -> None:
        import subprocess as sp

        with (
            patch(
                "holo.capabilities.shutil.which", return_value="/x/brew"
            ),
            patch(
                "holo.capabilities.subprocess.run",
                side_effect=sp.TimeoutExpired(cmd="brew", timeout=30),
            ),
        ):
            out = probe_packages()
        assert "brew" not in out


# ============================================================================
# CapabilitiesProbe (cache)
# ============================================================================


class TestCapabilitiesProbe:
    def test_collect_includes_top_level_fields(self) -> None:
        with (
            patch("holo.capabilities.shutil.which", return_value=None),
            patch("holo.capabilities.platform.system", return_value="Linux"),
        ):
            snap = CapabilitiesProbe().collect()
        assert snap["schema"] == CAPABILITIES_SCHEMA_VERSION
        assert "host" in snap
        assert "applications" in snap
        assert "packages" in snap
        assert "generated_at" in snap

    def test_no_software_kwargs(self) -> None:
        # Schema 2 dropped the curated software whitelist; the
        # constructor must reject the old kwargs to fail loudly on
        # callers using the old API.
        with pytest.raises(TypeError):
            CapabilitiesProbe(software=["ffmpeg"])  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            CapabilitiesProbe(packages=["brew"])  # type: ignore[call-arg]

    def test_cache_returns_same_dict_within_ttl(self) -> None:
        with (
            patch("holo.capabilities.shutil.which", return_value=None),
            patch("holo.capabilities.platform.system", return_value="Linux"),
        ):
            probe = CapabilitiesProbe(cache_ttl_s=60.0)
            a = probe.collect()
            b = probe.collect()
        assert a is b

    def test_cache_refreshes_after_ttl(self) -> None:
        with (
            patch("holo.capabilities.shutil.which", return_value=None),
            patch("holo.capabilities.platform.system", return_value="Linux"),
        ):
            probe = CapabilitiesProbe(cache_ttl_s=0.05)
            a = probe.collect()
            time.sleep(0.08)
            b = probe.collect()
        assert a is not b

    def test_force_bypasses_cache(self) -> None:
        with (
            patch("holo.capabilities.shutil.which", return_value=None),
            patch("holo.capabilities.platform.system", return_value="Linux"),
        ):
            probe = CapabilitiesProbe(cache_ttl_s=60.0)
            a = probe.collect()
            b = probe.collect(force=True)
        assert a is not b


# ============================================================================
# HTTP server (response shape changed; auth + no-CORS still apply)
# ============================================================================


@pytest.fixture
def caps_app() -> Any:
    from starlette.testclient import TestClient

    from holo.capabilities import CapabilitiesProbe
    from holo.capabilities_server import CapabilitiesServer

    server = CapabilitiesServer(
        probe=CapabilitiesProbe(),
        token="test-token-fixed",
    )
    client = TestClient(server._build_app())  # noqa: SLF001 — test surface
    return server, client


class TestCapabilitiesServerHTTP:
    def test_capabilities_returns_schema_2_shape(self, caps_app: Any) -> None:
        _, client = caps_app
        with (
            patch("holo.capabilities.shutil.which", return_value=None),
            patch("holo.capabilities.platform.system", return_value="Linux"),
        ):
            r = client.get(
                "/capabilities",
                headers={"X-Holo-Caps-Token": "test-token-fixed"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["schema"] == 2
        assert "applications" in body
        assert "packages" in body
        assert "software" not in body

    def test_no_cors_headers(self, caps_app: Any) -> None:
        _, client = caps_app
        with (
            patch("holo.capabilities.shutil.which", return_value=None),
            patch("holo.capabilities.platform.system", return_value="Linux"),
        ):
            r = client.get(
                "/capabilities",
                headers={"X-Holo-Caps-Token": "test-token-fixed"},
            )
        for header in (
            "access-control-allow-origin",
            "access-control-allow-headers",
            "access-control-allow-methods",
        ):
            assert header not in {k.lower() for k in r.headers}

    def test_401_without_token(self, caps_app: Any) -> None:
        _, client = caps_app
        r = client.get("/capabilities")
        assert r.status_code == 401

    def test_healthz_no_auth(self, caps_app: Any) -> None:
        _, client = caps_app
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
