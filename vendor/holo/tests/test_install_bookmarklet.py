"""Tests for `holo.install_bookmarklet`.

We don't make real network calls — `urllib.request.urlopen` and
`webbrowser.open` are patched. The goal is to verify the URL we
construct, the file we write, and the error paths.
"""

from __future__ import annotations

import io
import urllib.error
from unittest.mock import patch

from holo import __version__, install_bookmarklet


def test_release_url_uses_version():
    url = install_bookmarklet.release_url()
    assert url.endswith("holo-bookmarklet.html")
    assert f"/v{__version__}/" in url
    assert url.startswith("https://github.com/nospaceleftondevice/holo/releases/download")


def test_release_url_explicit_version():
    url = install_bookmarklet.release_url("9.9.9")
    assert "/v9.9.9/holo-bookmarklet.html" in url


class _FakeResponse:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_run_downloads_and_opens(tmp_path, monkeypatch):
    monkeypatch.setattr(install_bookmarklet.tempfile, "gettempdir", lambda: str(tmp_path))
    body = b"<html>holo bookmarklet</html>"

    captured: dict = {}

    def fake_urlopen(url, context=None):
        captured["url"] = url
        return _FakeResponse(body)

    def fake_open(uri):
        captured["uri"] = uri
        return True

    with patch.object(install_bookmarklet.urllib.request, "urlopen", fake_urlopen):
        with patch.object(install_bookmarklet.webbrowser, "open", fake_open):
            rc = install_bookmarklet.run()

    assert rc == 0
    dest = tmp_path / "holo-bookmarklet.html"
    assert dest.exists()
    assert dest.read_bytes() == body
    assert captured["url"] == install_bookmarklet.release_url()
    assert captured["uri"] == dest.as_uri()


def test_run_uses_explicit_url(tmp_path, monkeypatch):
    monkeypatch.setattr(install_bookmarklet.tempfile, "gettempdir", lambda: str(tmp_path))
    seen: dict = {}

    def fake_urlopen(url, context=None):
        seen["url"] = url
        return _FakeResponse(b"x")

    with patch.object(install_bookmarklet.urllib.request, "urlopen", fake_urlopen):
        with patch.object(install_bookmarklet.webbrowser, "open", lambda u: True):
            rc = install_bookmarklet.run(url="https://example.test/custom.html")

    assert rc == 0
    assert seen["url"] == "https://example.test/custom.html"


def test_run_returns_1_on_404_default_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install_bookmarklet.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake_urlopen(url, context=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with patch.object(install_bookmarklet.urllib.request, "urlopen", fake_urlopen):
        rc = install_bookmarklet.run()

    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 404" in err
    assert "--url" in err  # default-URL 404 points users at the override


def test_run_returns_1_on_404_explicit_url_no_upgrade_hint(tmp_path, monkeypatch, capsys):
    """When the user passed --url explicitly, the 'upgrade the binary'
    suggestion would be misleading, so it's suppressed."""
    monkeypatch.setattr(install_bookmarklet.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake_urlopen(url, context=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with patch.object(install_bookmarklet.urllib.request, "urlopen", fake_urlopen):
        rc = install_bookmarklet.run(url="https://example.test/missing.html")

    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 404" in err
    assert "upgrade the holo binary" not in err


def test_run_returns_1_on_url_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install_bookmarklet.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake_urlopen(url, context=None):
        raise urllib.error.URLError("name resolution failed")

    with patch.object(install_bookmarklet.urllib.request, "urlopen", fake_urlopen):
        rc = install_bookmarklet.run()

    assert rc == 1
    assert "name resolution failed" in capsys.readouterr().err


def test_run_returns_1_when_browser_unavailable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install_bookmarklet.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake_urlopen(url, context=None):
        return _FakeResponse(b"x")

    with patch.object(install_bookmarklet.urllib.request, "urlopen", fake_urlopen):
        with patch.object(install_bookmarklet.webbrowser, "open", lambda u: False):
            rc = install_bookmarklet.run()

    assert rc == 1
    assert "couldn't launch a browser" in capsys.readouterr().err
