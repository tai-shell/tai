"""`holo install-remote <user@host>` — install holo on an air-gapped
peer Mac over SSH.

Use case: the input-proxy peer Mac in a corporate environment where
either the locked Mac or the peer has no internet but they can reach
each other over the LAN. Run this from whichever side HAS internet (or
already has holo + the SikuliX jar cached). It SCPs the running
holo binary and the cached SikuliX jar to the target host, then runs
the install steps over SSH:

  - moves the binary into ~/bin/holo, chmod +x
  - codesign --force --deep --sign - (otherwise the macOS kernel
    rejects the distribution adhoc signature on first launch; same
    workaround `holo upgrade` already applies for the same reason)
  - drops the jar into ~/Library/Caches/holo/

Reports java availability on the remote (warns if missing, since
SikuliX's bridge needs OpenJDK 11+ and holo doesn't bundle a JDK).

macOS-only target — ~/Library/Caches paths are macOS conventions.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from holo import __version__

SIKULIX_JAR_NAME = "sikulixide-2.0.5.jar"
REMOTE_INSTALL_PATH = "~/bin/holo"  # path on the target; ~ expanded by remote shell


class InstallRemoteError(RuntimeError):
    """User-facing error from `holo install-remote`."""


def _local_holo() -> Path:
    """Return the path to the currently-running holo binary."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    found = shutil.which("holo")
    if found:
        return Path(found)
    raise InstallRemoteError(
        "could not find a `holo` binary to ship (running as a dev install "
        "and `holo` not on PATH). Build the PyInstaller binary first "
        "(`pyinstaller --clean holo.spec`) and put `dist/holo` on PATH."
    )


def _local_jar() -> Path:
    """Find the local SikuliX jar; download it via the bridge's
    `ensure_jar()` if not yet cached."""
    from holo.bridge import _user_cache_dir, ensure_jar

    jar = _user_cache_dir() / SIKULIX_JAR_NAME
    if jar.exists():
        return jar
    print(
        f"holo install-remote: SikuliX jar not yet cached at {jar} — "
        "fetching now (one-time ~123 MB download)…",
        flush=True,
    )
    return ensure_jar()


def _scp(local: Path, host: str, remote_path: str) -> None:
    cmd = ["scp", str(local), f"{host}:{remote_path}"]
    print(f"  {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise InstallRemoteError(
            f"scp failed (exit {result.returncode}); is `ssh {host}` "
            "working from this machine?"
        )


_REMOTE_INSTALL_SCRIPT = r"""
set -e
INSTALL_TARGET="$HOME/bin/holo"
JAR_DIR="$HOME/Library/Caches/holo"

mkdir -p "$(dirname "$INSTALL_TARGET")" "$JAR_DIR"
mv /tmp/holo "$INSTALL_TARGET"
chmod +x "$INSTALL_TARGET"
# Re-sign the binary locally — the distribution adhoc signature is
# rejected by the macOS kernel at load time on a fresh install
# (SIGKILL before any code runs). See holo PR #90.
codesign --force --deep --sign - "$INSTALL_TARGET"
mv /tmp/sikulixide-2.0.5.jar "$JAR_DIR/"

echo "----------------------------------------"
echo "holo $("$INSTALL_TARGET" --version) installed at $INSTALL_TARGET"
echo "SikuliX jar at $JAR_DIR/sikulixide-2.0.5.jar"
if command -v java >/dev/null; then
    echo "$(java -version 2>&1 | head -1)"
else
    echo "WARNING: java not on PATH — install OpenJDK 11+ before running"
    echo "         --screen tools. holo doesn't bundle a JDK."
fi
echo "----------------------------------------"
echo
echo "Start the input-proxy peer here with:"
echo "  $INSTALL_TARGET mcp --screen --no-bookmarklet --no-browser --listen 7081"
"""


def _ssh_install(host: str) -> int:
    """SSH to `host`, pipe the install script in over stdin, return the
    remote shell's exit code."""
    cmd = ["ssh", host, "bash", "-s"]
    print(f"  ssh {host} bash -s   (running install script)")
    result = subprocess.run(cmd, input=_REMOTE_INSTALL_SCRIPT, text=True)
    return result.returncode


def run(host: str) -> int:
    """Implements `holo install-remote <host>`. Returns CLI exit code."""
    if not host:
        print("holo install-remote: HOST is required", file=sys.stderr)
        return 2
    if "@" not in host:
        # Not fatal — ssh will use the current user, which may be right.
        print(
            f"note: HOST {host!r} has no user@ prefix; ssh will use the "
            "current user on this machine.",
            file=sys.stderr,
        )

    try:
        holo_local = _local_holo()
    except InstallRemoteError as e:
        print(f"holo install-remote: {e}", file=sys.stderr)
        return 1

    try:
        jar_local = _local_jar()
    except Exception as e:  # noqa: BLE001 — surface fetch errors verbatim
        print(f"holo install-remote: jar fetch failed: {e}", file=sys.stderr)
        return 1

    print(f"holo install-remote — installing holo {__version__} on {host}")
    print(f"  source binary: {holo_local}")
    print(f"  source jar:    {jar_local}")
    print(f"  remote target: {REMOTE_INSTALL_PATH}")
    print()

    try:
        print(f"→ copying holo binary to {host}…")
        _scp(holo_local, host, "/tmp/holo")
        print(f"→ copying SikuliX jar to {host}…")
        _scp(jar_local, host, "/tmp/sikulixide-2.0.5.jar")
        print(f"→ running install script on {host}…")
        rc = _ssh_install(host)
        if rc != 0:
            print(
                f"holo install-remote: remote install script exited {rc}",
                file=sys.stderr,
            )
            return 1
    except InstallRemoteError as e:
        print(f"holo install-remote: {e}", file=sys.stderr)
        return 1

    return 0
