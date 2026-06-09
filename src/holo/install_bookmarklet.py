"""Download `holo-bookmarklet.html` from the matching GitHub Release
and open it in the user's default browser.

Cross-platform: uses `webbrowser.open` (stdlib) so the same code path
works on macOS, Linux, and Windows. The `open` / `xdg-open` / `start`
shellouts that platform tutorials usually reach for are all proxied
through `webbrowser.open` under the hood.
"""

from __future__ import annotations

import ssl
import sys
import tempfile
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from holo import __version__

RELEASE_HOST = "https://github.com/nospaceleftondevice/holo/releases/download"
ASSET_NAME = "holo-bookmarklet.html"


def release_url(version: str = __version__) -> str:
    return f"{RELEASE_HOST}/v{version}/{ASSET_NAME}"


def _ssl_context() -> ssl.SSLContext:
    """Same rationale as `holo.bridge._ssl_context` — the PyInstaller-
    bundled OpenSSL points at CA paths from the build runner that don't
    exist on a fresh user machine, so explicitly use certifi's bundle
    when present."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download(url: str, dest: Path) -> None:
    ctx = _ssl_context()
    with urllib.request.urlopen(url, context=ctx) as response:  # noqa: S310 (pinned URL)
        with open(dest, "wb") as out:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)


def run(*, url: str | None = None) -> int:
    target_url = url if url else release_url()
    dest = Path(tempfile.gettempdir()) / ASSET_NAME

    print(f"holo install-bookmarklet — fetching {target_url}")
    try:
        _download(target_url, dest)
    except urllib.error.HTTPError as e:
        print(f"❌ download failed: HTTP {e.code} {e.reason}", file=sys.stderr)
        if e.code == 404 and url is None:
            print(
                f"   No `{ASSET_NAME}` for v{__version__}. The asset is attached "
                "to releases ≥ v0.1.0a3.",
                file=sys.stderr,
            )
            print(
                "   Either upgrade the holo binary, or pass --url to point at a "
                "specific release:",
                file=sys.stderr,
            )
            print(
                f"     holo install-bookmarklet --url {RELEASE_HOST}/<TAG>/{ASSET_NAME}",
                file=sys.stderr,
            )
        return 1
    except urllib.error.URLError as e:
        print(f"❌ download failed: {e.reason}", file=sys.stderr)
        return 1

    print(f"✓ saved to {dest}")
    print("Opening in default browser…")
    if not webbrowser.open(dest.as_uri()):
        # webbrowser.open returns False if no usable browser was found.
        # Tell the user the path so they can open it manually.
        print(
            f"⚠ couldn't launch a browser automatically. Open {dest} by hand.",
            file=sys.stderr,
        )
        return 1

    print()
    print("In the page that just opened, drag the 🔧 holo button to your")
    print("bookmarks bar. Then navigate to a normal http(s) page and click")
    print("the bookmark to calibrate.")
    return 0
