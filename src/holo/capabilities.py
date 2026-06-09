"""Cross-platform host-capabilities probe.

Three layers, all auto — no opt-in flags:

  1. **Hardware** — ``os``, ``os_version``, ``arch``, ``cpu_model``,
     ``cores``, ``ram_gb``. Cheap to gather; the routing-by-chip use
     case ("send transcription to the M4 host, not the M1") needs
     this layer.

  2. **Applications** — platform-native catalog of GUI apps:
       - macOS: walk ``/Applications``, ``/Applications/Utilities``,
         ``/System/Applications``, ``~/Applications``, then merge in
         results from ``mdfind 'kMDItemKind == "Application"'`` to
         pick up ``.app`` bundles outside the standard dirs.
       - Windows: query the ``HKLM`` and ``HKCU`` Uninstall registry
         keys (canonical "installed programs" list).
       - Linux: empty — desktop apps are delivered via the package
         managers below, not as a separate catalog.

  3. **Packages** — auto-run every supported package manager whose
     binary is present on the host. Output is a per-manager array
     of ``{name, version}`` records:
       - macOS: ``brew``, ``port``
       - Linux OS-supplied: ``apt``/``dpkg``, ``dnf``/``yum``/``rpm``,
         ``pacman``
       - Linux third-party: ``snap``, ``flatpak``, ``brew`` (linuxbrew)
       - Windows: ``winget``, ``choco``, ``scoop``
       - Cross-platform language-level: ``pip``, ``pipx``, ``cargo``,
         ``npm`` (global), ``gem``, ``conda`` (base env)

Walking ``$PATH`` directly (the previous design) was wrong on Linux:
``/usr/bin`` is where apt installs everything AND where the OS ships
its baseline binaries. Filtering one would lose the other. Trusting
the package managers as the source of truth solves this — apt-installed
``ffmpeg`` shows up in ``packages.apt`` even though ``/usr/bin`` is
no longer walked.

Results are cached for ``cache_ttl_s`` seconds — the slow probes
(brew list, pip list, winget list, conda list) take 1-5 s each and
the capabilities endpoint is meant to be polled.

The probe is read-only and never installs / modifies anything; it
only inspects the host. The data is meant to be served over the
authenticated capabilities HTTP endpoint
(:mod:`holo.capabilities_server`), which a discovering agent fetches
to decide where to route a task.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import plistlib
import shutil
import subprocess
import threading
import time
from typing import Any

_log = logging.getLogger(__name__)


# Schema version of the JSON returned by `CapabilitiesProbe.collect()`.
# Bumped to 2 in 0.1.0a16 when the curated `software` field was removed
# in favour of platform-native `applications` + auto-run `packages`.
# Bump again only on incompatible structural changes; new optional
# fields stay safe under the same version.
CAPABILITIES_SCHEMA_VERSION = 2


# ============================================================================
# Package-manager probes
#
# Each `_probe_X` returns one of:
#   - list[dict[name, version]]  on success (possibly empty)
#   - None                       when the manager isn't installed or its
#                                command failed; the result dict will
#                                omit the key entirely
# ============================================================================


def _probe_brew() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["brew", "list", "--versions"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        # `brew list --versions` emits `name v1 v2 v3` for kegs with
        # multiple installed versions; we canonicalize on the first
        # (newest-listed). Good enough for routing decisions.
        if len(parts) >= 2:
            pkgs.append({"name": parts[0], "version": parts[1]})
        else:
            pkgs.append({"name": parts[0], "version": ""})
    return pkgs


def _probe_apt() -> list[dict[str, str]] | None:
    # `dpkg-query` is more deterministic than `apt list --installed`,
    # which prints a "WARNING: ..." line on stderr that some shells
    # surface. Same data, cleaner output.
    out = _run_pkg_command(
        ["dpkg-query", "-W", "-f=${Package} ${Version}\n"]
    )
    if out is None:
        return None
    return _parse_two_column(out)


def _probe_rpm() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["rpm", "-qa", "--qf", "%{NAME} %{VERSION}\n"])
    if out is None:
        return None
    return _parse_two_column(out)


def _probe_port() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["port", "installed"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    for raw in out.splitlines():
        # `port installed` indents package lines; the header line
        # ("The following ports are currently installed:") starts at
        # column 0, so we filter it out by indentation.
        if not raw.startswith(" "):
            continue
        body = raw.strip()
        if " @" not in body:
            continue
        name, rest = body.split(" @", 1)
        # `@7.0.1_0 (active)` → "7.0.1"
        version = rest.split()[0].split("_", 1)[0]
        pkgs.append({"name": name, "version": version})
    return pkgs


def _probe_pacman() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["pacman", "-Q"])
    if out is None:
        return None
    return _parse_two_column(out)


def _probe_snap() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["snap", "list"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    # `snap list` first line is a header:
    #   Name   Version   Rev   Tracking   Publisher   Notes
    # Skip it.
    for raw in out.splitlines()[1:]:
        parts = raw.split()
        if len(parts) >= 2:
            pkgs.append({"name": parts[0], "version": parts[1]})
    return pkgs


def _probe_flatpak() -> list[dict[str, str]] | None:
    # `--columns=application,version` makes the output deterministic
    # and tab-separated, regardless of the user's locale.
    out = _run_pkg_command(
        ["flatpak", "list", "--columns=application,version"]
    )
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    for raw in out.splitlines():
        if not raw.strip():
            continue
        # Tab-separated when --columns is used.
        parts = raw.split("\t")
        if len(parts) >= 2:
            pkgs.append(
                {"name": parts[0].strip(), "version": parts[1].strip()}
            )
        elif parts:
            pkgs.append({"name": parts[0].strip(), "version": ""})
    return pkgs


def _probe_winget() -> list[dict[str, str]] | None:
    out = _run_pkg_command(
        ["winget", "list", "--accept-source-agreements"],
        # winget's first invocation can be slow due to source agreement
        # negotiation; give it longer than the default 30 s.
        timeout=60.0,
    )
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    # winget output is human-formatted with a header bar of `---`. We
    # skip lines until we see the bar, then take everything after.
    seen_bar = False
    for raw in out.splitlines():
        if not seen_bar:
            if raw.strip().startswith("---"):
                seen_bar = True
            continue
        cells = raw.split()
        if not cells:
            continue
        # Best-effort: first cell = name, third = version.
        name = cells[0]
        version = cells[2] if len(cells) >= 3 else ""
        pkgs.append({"name": name, "version": version})
    return pkgs


def _probe_choco() -> list[dict[str, str]] | None:
    # `-r` (raw) yields `name|version` per line — much easier than the
    # default human format.
    out = _run_pkg_command(["choco", "list", "--local-only", "-r"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        name, _, version = line.partition("|")
        pkgs.append({"name": name, "version": version})
    return pkgs


def _probe_scoop() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["scoop", "list"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    seen_bar = False
    for raw in out.splitlines():
        if not seen_bar:
            if raw.strip().startswith("---"):
                seen_bar = True
            continue
        parts = raw.split()
        if len(parts) >= 2:
            pkgs.append({"name": parts[0], "version": parts[1]})
    return pkgs


def _probe_pip() -> list[dict[str, str]] | None:
    # `pip` may not be on PATH (e.g. `pip3` only). Try `pip` first, then
    # `pip3`, then `python3 -m pip` as last resort. Use `--format=freeze`
    # for the simplest deterministic output (`name==version`) — JSON
    # format requires `pkg_resources` which can be slow on cold caches.
    for cmd in (
        ["pip", "list", "--format=freeze", "--disable-pip-version-check"],
        ["pip3", "list", "--format=freeze", "--disable-pip-version-check"],
        [
            "python3",
            "-m",
            "pip",
            "list",
            "--format=freeze",
            "--disable-pip-version-check",
        ],
    ):
        out = _run_pkg_command(cmd)
        if out is not None:
            return _parse_pip_freeze(out)
    return None


def _parse_pip_freeze(text: str) -> list[dict[str, str]]:
    pkgs: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # `pip freeze` lines look like `name==1.2.3` or `name @ file:///`
        # for editable installs. We only care about the name + version.
        if "==" in line:
            name, _, version = line.partition("==")
            pkgs.append(
                {"name": name.strip(), "version": version.strip()}
            )
        elif " @ " in line:
            name = line.partition(" @ ")[0].strip()
            pkgs.append({"name": name, "version": ""})
    return pkgs


def _probe_pipx() -> list[dict[str, str]] | None:
    # `pipx list --short` outputs `name version` per line — the
    # cleanest format pipx exposes.
    out = _run_pkg_command(["pipx", "list", "--short"])
    if out is None:
        return None
    return _parse_two_column(out)


def _probe_cargo() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["cargo", "install", "--list"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    # Output:
    #   package vX.Y.Z:
    #       binary1
    #       binary2
    # We only want the package lines (unindented, end with colon).
    for raw in out.splitlines():
        if raw.startswith(" "):
            continue
        line = raw.rstrip(":").strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            name = parts[0]
            # Strip the leading 'v' from "v1.2.3".
            version = parts[1].lstrip("v")
            pkgs.append({"name": name, "version": version})
    return pkgs


def _probe_npm() -> list[dict[str, str]] | None:
    # `npm list -g --depth=0 --json` returns JSON; safer than parsing
    # the human-formatted tree output.
    out = _run_pkg_command(
        ["npm", "list", "-g", "--depth=0", "--json"],
        timeout=60.0,
    )
    if out is None:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    deps = data.get("dependencies") or {}
    pkgs: list[dict[str, str]] = []
    for name, info in deps.items():
        if not isinstance(info, dict):
            continue
        version = str(info.get("version", ""))
        pkgs.append({"name": name, "version": version})
    return pkgs


def _probe_gem() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["gem", "list", "--local"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    # `gem list` lines look like `rake (13.0.6, 13.0.3)`.
    for raw in out.splitlines():
        line = raw.strip()
        if not line or " (" not in line:
            continue
        name, rest = line.split(" (", 1)
        # First version listed is the highest installed; take that.
        version = rest.rstrip(")").split(",", 1)[0].strip()
        pkgs.append({"name": name.strip(), "version": version})
    return pkgs


def _probe_conda() -> list[dict[str, str]] | None:
    # Probe only the `base` env. Per-env enumeration is out of scope —
    # most users share a single env for tools they want discovered.
    out = _run_pkg_command(
        ["conda", "list", "-n", "base", "--json"], timeout=60.0
    )
    if out is None:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    pkgs: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        pkgs.append({"name": name, "version": str(item.get("version", ""))})
    return pkgs


# Dispatch table — every supported package manager. Auto-detected by
# the binary's presence on PATH (`shutil.which` inside `_run_pkg_command`).
# Aliases (`dpkg` → apt probe; `dnf`/`yum` → rpm probe) collapse onto
# their backing query commands. The output dict uses the canonical
# manager name as the key.
_PKG_PROBES: dict[str, Any] = {
    # macOS
    "brew": _probe_brew,
    "port": _probe_port,
    # Linux OS-supplied
    "apt": _probe_apt,
    "dnf": _probe_rpm,
    "yum": _probe_rpm,
    "rpm": _probe_rpm,
    "pacman": _probe_pacman,
    # Linux third-party
    "snap": _probe_snap,
    "flatpak": _probe_flatpak,
    # Windows
    "winget": _probe_winget,
    "choco": _probe_choco,
    "scoop": _probe_scoop,
    # Cross-platform / language-level
    "pip": _probe_pip,
    "pipx": _probe_pipx,
    "cargo": _probe_cargo,
    "npm": _probe_npm,
    "gem": _probe_gem,
    "conda": _probe_conda,
}


# Aliases that share an underlying probe; we don't want to emit them
# twice in the response. e.g. `dpkg` resolves to the same dpkg-query
# call as `apt` — only `apt` shows up.
_PROBE_ALIASES: dict[str, str] = {
    "dpkg": "apt",
    "dnf": "rpm",
    "yum": "rpm",
}


SUPPORTED_PKG_MANAGERS: tuple[str, ...] = tuple(_PKG_PROBES)


def _parse_two_column(text: str) -> list[dict[str, str]]:
    """Parse `name version` whitespace-separated lines into entries."""
    pkgs: list[dict[str, str]] = []
    for raw in text.splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) == 2:
            pkgs.append({"name": parts[0], "version": parts[1]})
        elif len(parts) == 1 and parts[0]:
            pkgs.append({"name": parts[0], "version": ""})
    return pkgs


def _run_pkg_command(
    cmd: list[str], timeout: float = 30.0
) -> str | None:
    """Run a probe command; return stdout, or None if unavailable.

    None signals "manager not installed / probe failed" — the caller
    omits the entry entirely. An empty stdout returns ``""`` and the
    probe parser yields ``[]``, which DOES end up in the result dict
    (distinct from None).
    """
    if shutil.which(cmd[0]) is None:
        return None
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _log.debug("capabilities: %s probe failed: %s", cmd[0], e)
        return None
    if result.returncode != 0:
        _log.debug(
            "capabilities: %s probe exited %d: %s",
            cmd[0],
            result.returncode,
            (result.stderr or "").strip()[:200],
        )
        return None
    return result.stdout


# ============================================================================
# Hardware
# ============================================================================


def probe_hardware() -> dict[str, Any]:
    """Collect static host facts: OS, arch, CPU, RAM."""
    return {
        "os": platform.system().lower(),  # darwin / linux / windows
        "os_version": _os_version(),
        "arch": platform.machine().lower(),  # arm64 / x86_64 / amd64
        "cpu_model": _cpu_model(),
        "cores": os.cpu_count() or 0,
        "ram_gb": _ram_gb(),
    }


def _os_version() -> str:
    """Return the user-meaningful OS version string."""
    system = platform.system()
    if system == "Darwin":
        # `platform.release()` returns the Darwin kernel version
        # (e.g. "24.0.0") on macOS, which means nothing to humans.
        # `mac_ver()` is supposed to return the marketing version
        # ("14.5") but lies on Pythons built against pre-Big-Sur SDKs
        # (Anaconda 3.13 returns "10.16" here even on Sequoia). Shell
        # out to `sw_vers` first — it reads
        # /System/Library/CoreServices/SystemVersion.plist directly and
        # returns the actual product version regardless of binary
        # compat shims.
        try:
            r = subprocess.run(
                ["sw_vers", "-productVersion"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if r.returncode == 0:
                out = r.stdout.strip()
                if out:
                    return out
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            mv = platform.mac_ver()[0]
            if mv:
                return mv
        except Exception:  # noqa: BLE001 — defensive; mac_ver shouldn't raise
            pass
    return platform.release()


def _cpu_model() -> str:
    """Best-effort CPU brand string per platform."""
    system = platform.system()
    if system == "Darwin":
        try:
            r = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if r.returncode == 0:
                out = r.stdout.strip()
                if out:
                    return out
        except (OSError, subprocess.SubprocessError):
            pass
    if system == "Linux":
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or ""


def _ram_gb() -> float:
    """Total physical RAM in GiB (rounded to 1 decimal)."""
    system = platform.system()
    if system == "Darwin":
        try:
            r = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if r.returncode == 0:
                bytes_ = int(r.stdout.strip())
                return round(bytes_ / 1024**3, 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if system == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / 1024**2, 1)
        except (OSError, ValueError):
            pass
    if system == "Windows":
        try:
            return _windows_ram_gb()
        except Exception:  # noqa: BLE001 — defensive; windows-only path
            pass
    return 0.0


def _windows_ram_gb() -> float:
    """Read total physical RAM via the Win32 API."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))  # type: ignore[attr-defined]
    return round(m.ullTotalPhys / 1024**3, 1)


# ============================================================================
# Applications (platform-native catalog)
# ============================================================================


# macOS .app bundle directories. We walk these in order and dedupe by
# bundle name. ~/Applications is an Apple-blessed per-user location for
# drag-installed apps; included for completeness even though it's empty
# on most Macs.
_MACOS_APP_DIRS: tuple[str, ...] = (
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "~/Applications",
)


def probe_applications() -> dict[str, dict[str, str]]:
    """Enumerate platform-native applications.

    macOS: walk standard ``.app`` dirs + run ``mdfind`` to pick up
    bundles outside those dirs (e.g. ~/Library/Application Support
    helpers, custom install locations). Each entry is
    ``"<DisplayName>": {"path": "/Applications/X.app"}``.

    Windows: enumerate the Uninstall registry keys under HKLM and
    HKCU. Each entry is ``"<DisplayName>": {"path", "version",
    "publisher"}`` — richer metadata than macOS gives us for free.

    Linux: empty — desktop apps are delivered via package managers
    (snap/flatpak/apt/dnf), which the ``packages`` layer covers.
    """
    system = platform.system()
    if system == "Darwin":
        return _probe_applications_macos()
    if system == "Windows":
        return _probe_applications_windows()
    return {}


def _probe_applications_macos() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}

    for raw_dir in _MACOS_APP_DIRS:
        d = os.path.expanduser(raw_dir)
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        try:
            for entry in entries:
                if not entry.name.endswith(".app"):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=True)
                except OSError:
                    continue
                if not is_dir:
                    continue
                name = entry.name[: -len(".app")]
                if name not in out:
                    out[name] = _build_app_entry(entry.path)
        finally:
            entries.close()

    # `mdfind` consults the Spotlight index; fast and covers .app
    # bundles outside the standard dirs (browser-installed PWAs,
    # development builds, helper apps that drop in oddball locations).
    # Existing entries take precedence so the canonical /Applications
    # path wins on duplicates.
    #
    # We aggressively skip paths under `/System/Library/` and
    # `/Library/` — those are private OS agents (FaceTimeAgent.app,
    # 50onPaletteServer.app, hundreds of others) that the user has
    # never seen and never installed. Routing decisions don't care
    # about them and the noise blows the response size up by 10x.
    seen_paths = {info["path"] for info in out.values()}
    md = _run_pkg_command(
        ["mdfind", "kMDItemKind == \"Application\""], timeout=10.0
    )
    if md is not None:
        for raw in md.splitlines():
            path = raw.strip()
            if not path or not path.endswith(".app"):
                continue
            if _is_system_app_bundle(path):
                continue
            if path in seen_paths:
                continue
            name = os.path.basename(path)[: -len(".app")]
            if name in out:
                continue
            out[name] = _build_app_entry(path)
            seen_paths.add(path)
    return out


def _build_app_entry(bundle_path: str) -> dict[str, str]:
    """Assemble the per-app dict — `path` plus whatever Info.plist gives us."""
    entry: dict[str, str] = {"path": bundle_path}
    entry.update(_read_app_metadata(bundle_path))
    return entry


def _read_app_metadata(bundle_path: str) -> dict[str, str]:
    """Pull `version` + `bundle_id` from a `.app` bundle's Info.plist.

    Returns whatever keys we can extract; missing keys are omitted so
    the merged entry stays tight (no nulls for unread fields). Read
    failures (corrupt plist, permission denied, missing file) yield
    an empty dict — the caller still has `path` and the entry shows
    up in the response with version omitted, which the agent
    interprets as "version unknown" rather than "not installed".

    We prefer ``CFBundleShortVersionString`` (the marketing version,
    e.g. "125.0.6422.142") over ``CFBundleVersion`` (the build number,
    sometimes the same, sometimes a much-shorter monotonic counter).
    Routing decisions usually want the marketing version because
    that's what the user / docs reference.
    """
    plist_path = os.path.join(bundle_path, "Contents", "Info.plist")
    try:
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
    except Exception as e:  # noqa: BLE001 — any read failure → "metadata unknown"
        # Real-world failures we've hit:
        #   * `xml.parsers.expat.ExpatError` — old-style ASCII (NeXTSTEP)
        #     plists ({ key = "value"; } syntax) that plistlib can't
        #     parse. Apple still ships some bundles in this format.
        #   * `plistlib.InvalidFileException` — corrupt binary plist.
        #   * `OSError` — missing or permission-denied Info.plist.
        # All of these collapse to "no metadata for this app", and the
        # caller still surfaces the entry with `path` populated. Logged
        # at DEBUG so a verbose run can list which plists were skipped.
        _log.debug(
            "capabilities: failed to read Info.plist for %s: %s",
            bundle_path,
            e,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    version = data.get("CFBundleShortVersionString") or data.get(
        "CFBundleVersion"
    )
    if version:
        out["version"] = str(version)
    bundle_id = data.get("CFBundleIdentifier")
    if bundle_id:
        out["bundle_id"] = str(bundle_id)
    return out


def _is_system_app_bundle(path: str) -> bool:
    """True for `.app` paths that are private OS agents users never see.

    macOS ships hundreds of these under `/System/Library/...`,
    `/Library/...`, and `/usr/libexec/` — they're internal helper
    apps (FaceTime agents, palette servers, accessibility shims) that
    contribute massive noise to the applications dict and aren't
    relevant to capability routing. The user-facing stock Apple apps
    (Safari, Mail, Calendar, etc.) live under `/System/Applications/`
    and are picked up by the directory walk above.
    """
    return (
        path.startswith("/System/Library/")
        or path.startswith("/Library/")
        or path.startswith("/usr/libexec/")
    )


def _probe_applications_windows() -> dict[str, dict[str, str]]:
    """Read the Uninstall registry keys.

    Three roots cover most cases:
      - HKLM\\…\\Uninstall — system-wide installers
      - HKLM\\…\\WOW6432Node\\…\\Uninstall — 32-bit installers on 64-bit Windows
      - HKCU\\…\\Uninstall — per-user installers

    Per-key fields we care about: DisplayName, DisplayVersion,
    Publisher, InstallLocation. Skip entries without DisplayName
    (Windows updates etc).
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return {}

    out: dict[str, dict[str, str]] = {}
    roots = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
    ]
    for hive, subkey in roots:
        try:
            root = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    sub = winreg.OpenKey(root, name)
                except OSError:
                    continue
                try:
                    entry: dict[str, str] = {}
                    for src, dst in (
                        ("DisplayName", "name"),
                        ("DisplayVersion", "version"),
                        ("Publisher", "publisher"),
                        ("InstallLocation", "path"),
                        # Registry stores InstallDate as REG_SZ in the
                        # form YYYYMMDD (e.g. "20251102") for most
                        # MSI-installed packages. Some installers omit
                        # it entirely; we just pass through whatever's
                        # there — no parsing.
                        ("InstallDate", "install_date"),
                    ):
                        try:
                            value, _ = winreg.QueryValueEx(sub, src)
                        except OSError:
                            continue
                        if value is None:
                            continue
                        entry[dst] = str(value)
                finally:
                    sub.Close()
                if "name" not in entry:
                    continue
                display = entry.pop("name")
                if display in out:
                    continue
                # Always include a `path` key so the response shape is
                # uniform with macOS — fall back to install-location if
                # set, else empty string.
                entry.setdefault("path", "")
                out[display] = entry
        finally:
            root.Close()
    return out


# ============================================================================
# Packages (auto-run every supported probe whose binary is on PATH)
# ============================================================================


def probe_packages() -> dict[str, list[dict[str, str]]]:
    """Run every supported probe; collect results from those installed.

    Auto-detected — no opt-in. Probes whose binary isn't on PATH return
    None and are omitted from the output dict. The result keys are the
    canonical manager names (aliases collapse onto their backing probe).
    """
    out: dict[str, list[dict[str, str]]] = {}
    for name, probe in _PKG_PROBES.items():
        if name in _PROBE_ALIASES:
            # Alias of another probe — already handled by the canonical
            # entry. Skip to avoid emitting duplicate keys.
            continue
        try:
            result = probe()
        except Exception:  # noqa: BLE001 — keep going on partial failure
            _log.exception(
                "capabilities: %s probe raised; skipping", name
            )
            continue
        if result is not None:
            out[name] = result
    return out


# ============================================================================
# Aggregator
# ============================================================================


class CapabilitiesProbe:
    """Owns a TTL cache around the full probe sweep.

    The cache exists because the slow probes (brew, pip, npm, conda,
    winget) can take several seconds each on cold caches. The
    capabilities endpoint is meant to be polled, so amortizing the
    cost across requests is what makes that workable.
    """

    def __init__(self, *, cache_ttl_s: float = 60.0) -> None:
        self._cache_ttl_s = float(cache_ttl_s)
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any]] | None = None

    def collect(self, *, force: bool = False) -> dict[str, Any]:
        """Return the cached capability snapshot, refreshing if stale."""
        with self._lock:
            now = time.monotonic()
            if not force and self._cached is not None:
                ts, data = self._cached
                if now - ts < self._cache_ttl_s:
                    return data
            data = self._collect_locked()
            self._cached = (now, data)
            return data

    def _collect_locked(self) -> dict[str, Any]:
        # Hold the lock through the full sweep. They're slow but the
        # alternative — release-and-reacquire — would let two concurrent
        # callers double-probe, defeating the cache.
        return {
            "schema": CAPABILITIES_SCHEMA_VERSION,
            "host": probe_hardware(),
            "applications": probe_applications(),
            "packages": probe_packages(),
            "generated_at": int(time.time()),
        }


__all__ = [
    "CAPABILITIES_SCHEMA_VERSION",
    "SUPPORTED_PKG_MANAGERS",
    "CapabilitiesProbe",
    "probe_applications",
    "probe_hardware",
    "probe_packages",
]
