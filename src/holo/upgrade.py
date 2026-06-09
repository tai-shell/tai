"""`holo upgrade` — fetch the latest GitHub release binary and replace
the running holo executable in place.

Also exposes `check_for_update()` for the implicit notice the bare
`holo` invocation prints under its help screen. That call is cached
on disk (24h TTL) so the help command isn't gated on a GitHub round
trip every time.

Release model: all holo tags follow PEP 440 pre-release form
(`v0.1.0aN`, eventually `v0.1.0`, `v0.1.0rc1`, etc.). GitHub's
`releases/latest` endpoint hides pre-releases — and right now every
holo release IS a pre-release — so we walk `/releases?per_page=N` and
pick the highest `v*` tag whose version parses cleanly. Non-version
tags (vendored asset releases like `vendored-sikulix-2.0.5`) are
skipped.

Asset naming matches `.github/workflows/release.yml`:
  macOS  → holo-macos-universal2
  Linux  → holo-linux-x86_64
  Win    → holo-windows-x86_64.exe
"""

from __future__ import annotations

import json
import os
import platform
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from holo import __version__

REPO = "nospaceleftondevice/holo"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
RELEASE_DOWNLOAD = f"https://github.com/{REPO}/releases/download"

# How long a cached "latest tag" check stays valid for the implicit
# no-args notice. `holo upgrade` always ignores the cache.
CACHE_TTL_SECONDS = 24 * 3600


class UpgradeError(Exception):
    """User-facing upgrade failure. The CLI prints `str(e)` and exits 1."""


# --- platform/install detection -------------------------------------------

def asset_name() -> str:
    """Release asset filename for the running platform."""
    if sys.platform == "darwin":
        return "holo-macos-universal2"
    if sys.platform.startswith("linux"):
        # Only x86_64 is built today; arm64 Linux would need a new CI
        # target. Surface a clear error rather than silently grabbing
        # an x86_64 binary the user can't run.
        machine = platform.machine().lower()
        if machine not in ("x86_64", "amd64"):
            raise UpgradeError(
                f"no holo release asset for linux/{machine} "
                "(only linux-x86_64 is currently built)"
            )
        return "holo-linux-x86_64"
    if sys.platform == "win32":
        return "holo-windows-x86_64.exe"
    raise UpgradeError(f"unsupported platform: {sys.platform}")


def install_path() -> Path:
    """Path to the running holo binary.

    PyInstaller sets `sys.frozen = True` and `sys.executable` points at
    the bundled binary. In a dev install (`pip install -e .`), the
    `holo` entry point is a Python wrapper script — replacing it with a
    PyInstaller binary would break the dev install, so refuse.
    """
    if not getattr(sys, "frozen", False):
        raise UpgradeError(
            "holo upgrade only supports binary releases — this looks "
            "like a dev install (running from `pip install -e .`). "
            "Use `git pull` and rebuild instead."
        )
    return Path(sys.executable)


# --- version parsing ------------------------------------------------------

# PEP 440 lite: `1.2.3`, `1.2.3a4`, `1.2.3b2`, `1.2.3rc1`, `1.2.3.post1`.
# `dev` segments aren't published by the release workflow, so unsupported.
_RELEASE_KIND_ORDER = {"a": 0, "b": 1, "rc": 2, "": 3, "post": 4}
_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+)|\.post(\d+))?$"
)


def parse_version(raw: str) -> tuple | None:
    """Return a comparable tuple for `raw`, or None if it doesn't parse.

    The leading `v` is optional. Ordering: alpha < beta < rc < release <
    post, with each pre-release's numeric suffix breaking ties within a
    base version.
    """
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    kind = m.group(4) or ""
    if kind:
        suffix = int(m.group(5))
    elif m.group(6) is not None:  # .postN
        kind = "post"
        suffix = int(m.group(6))
    else:
        suffix = 0
    return (major, minor, patch, _RELEASE_KIND_ORDER[kind], suffix)


# --- HTTP -----------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _http_get_json(url: str, *, timeout: float) -> object:
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(  # noqa: S310 (pinned URL)
        req, context=_ssl_context(), timeout=timeout
    ) as response:
        return json.loads(response.read())


def _http_download(url: str, dest: Path, *, timeout: float) -> None:
    with urllib.request.urlopen(  # noqa: S310 (pinned URL)
        url, context=_ssl_context(), timeout=timeout
    ) as response:
        with open(dest, "wb") as out:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)


def fetch_latest_tag(*, timeout: float = 5.0) -> str:
    """Return the highest `v*` tag on GitHub, e.g. `v0.1.0a24`.

    Raises UpgradeError on network failure or if no holo version tag
    is present in the API response window.
    """
    try:
        releases = _http_get_json(RELEASES_API, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise UpgradeError(f"GitHub releases API: HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise UpgradeError(f"GitHub releases API: {e.reason}") from e
    except TimeoutError as e:  # urlopen timeout surfaces as URLError on 3.10+
        raise UpgradeError(f"GitHub releases API: timeout ({e})") from e

    if not isinstance(releases, list):
        raise UpgradeError("GitHub releases API returned non-list payload")

    best: tuple | None = None
    best_tag: str | None = None
    for entry in releases:
        tag = entry.get("tag_name") if isinstance(entry, dict) else None
        if not isinstance(tag, str):
            continue
        v = parse_version(tag)
        if v is None:
            continue
        if best is None or v > best:
            best = v
            best_tag = tag

    if best_tag is None:
        raise UpgradeError(
            "no holo version tags found on GitHub (looked at first "
            f"{len(releases)} releases)"
        )
    return best_tag


# --- cache for no-args notice ---------------------------------------------

def cache_dir() -> Path:
    """Platform cache directory for holo."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "holo"


def _cache_path() -> Path:
    return cache_dir() / "version_check.json"


def _read_cache() -> tuple[float, str] | None:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return None
    ts = data.get("checked_at")
    tag = data.get("tag")
    if not isinstance(ts, (int, float)) or not isinstance(tag, str):
        return None
    return float(ts), tag


def _write_cache(tag: str) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": time.time(), "tag": tag}))
    except OSError:
        # Cache is best-effort — never fail the caller over a write error.
        pass


def check_for_update(*, timeout: float = 1.0, use_cache: bool = True) -> str | None:
    """Return the latest tag if it's strictly newer than `__version__`,
    else None. Network/cache errors return None silently — this is used
    on the help screen and must never break it.
    """
    now = time.time()
    cached = _read_cache() if use_cache else None

    tag: str | None = None
    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
        tag = cached[1]
    else:
        try:
            tag = fetch_latest_tag(timeout=timeout)
        except UpgradeError:
            return None
        _write_cache(tag)

    current = parse_version(__version__)
    latest = parse_version(tag) if tag else None
    if current is None or latest is None:
        return None
    if latest > current:
        return tag
    return None


# --- upgrade flow ---------------------------------------------------------

def _maybe_codesign_macos(path: Path) -> None:
    """Re-sign downloaded binary on macOS to dodge the kernel's load-time
    code-signature rejection (see PR #90). Releases built after #90 are
    already correctly re-signed in CI so this is a redundant no-op;
    older releases — and any future regression — still need it. Failure
    here is non-fatal; the upgrade prints a warning and continues.
    """
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(path)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(
            f"warning: codesign re-sign failed ({e}); the binary may be "
            "killed by the kernel on load. Run "
            f"`codesign --force --deep --sign - {path}` by hand if so.",
            file=sys.stderr,
        )


def run_upgrade(*, force: bool = False) -> int:
    """Implements `holo upgrade`. Returns CLI exit code."""
    try:
        target = install_path()
        asset = asset_name()
    except UpgradeError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    print(f"holo upgrade — currently installed: v{__version__} at {target}")

    try:
        latest_tag = fetch_latest_tag(timeout=10.0)
    except UpgradeError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    latest = parse_version(latest_tag)
    current = parse_version(__version__)
    if latest is None:
        print(f"❌ could not parse latest tag {latest_tag!r}", file=sys.stderr)
        return 1
    if current is not None and latest <= current and not force:
        print(f"✓ already up to date (latest: {latest_tag})")
        return 0

    url = f"{RELEASE_DOWNLOAD}/{latest_tag}/{asset}"
    print(f"  latest: {latest_tag}")
    print(f"  asset:  {asset}")
    print(f"  url:    {url}")

    target_dir = target.parent
    if not os.access(target_dir, os.W_OK):
        print(
            f"❌ no write permission for {target_dir}. Re-run as the "
            "owner of that directory (e.g. with sudo) or move the binary "
            "to a user-owned path first.",
            file=sys.stderr,
        )
        return 1

    # Stage next to the target so `os.replace` is atomic (same fs). Use
    # a NamedTemporaryFile only for unique naming — we manage cleanup
    # explicitly because the rename consumes the file.
    fd, tmp_str = tempfile.mkstemp(
        prefix=".holo-upgrade-", dir=str(target_dir)
    )
    os.close(fd)
    tmp = Path(tmp_str)

    try:
        print("  downloading…")
        _http_download(url, tmp, timeout=60.0)
        # PyInstaller artifacts on Unix need +x; the source zip won't
        # have it. Match the binary's mode if possible, else 0o755.
        try:
            mode = target.stat().st_mode & 0o777
        except OSError:
            mode = 0o755
        os.chmod(tmp, mode | 0o111)
        _maybe_codesign_macos(tmp)

        if sys.platform == "win32":
            # Windows can't overwrite a running .exe directly. Rename
            # the in-place binary aside; the OS will delete it next
            # boot or the next upgrade can clean it up.
            stash = target.with_suffix(target.suffix + ".old")
            if stash.exists():
                try:
                    stash.unlink()
                except OSError:
                    pass
                try:
                    target.replace(stash)
                except OSError as e:
                    print(f"❌ could not move running binary aside: {e}", file=sys.stderr)
                    return 1
            else:
                target.replace(stash)
            tmp.replace(target)
        else:
            os.replace(tmp, target)
    except urllib.error.HTTPError as e:
        print(f"❌ download failed: HTTP {e.code} {e.reason}", file=sys.stderr)
        _safe_unlink(tmp)
        return 1
    except urllib.error.URLError as e:
        print(f"❌ download failed: {e.reason}", file=sys.stderr)
        _safe_unlink(tmp)
        return 1
    except OSError as e:
        print(f"❌ install failed: {e}", file=sys.stderr)
        _safe_unlink(tmp)
        return 1

    # Invalidate the cache so the next no-args run doesn't keep
    # nagging about the version we just installed.
    try:
        _cache_path().unlink()
    except OSError:
        pass

    print(f"✓ upgraded to {latest_tag}")
    print(f"  binary: {target}")
    return 0


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
