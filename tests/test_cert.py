"""Unit tests for holo.cert.

Pin: path mapping, backend resolution precedence, keypair generation
(via mocked ssh-keygen), /v1/ssh/sign request shape + error handling,
on-disk cert+meta save, status inspection, refresh-threshold logic,
and the orchestrating ``get_or_refresh`` end-to-end flow.

We don't shell out to a real ssh-keygen — that's covered by manual
smoke testing. The mocked subprocess.run lets us assert the exact
flag set ssh-keygen receives so a future swap to a Python-native
keypair generator (cryptography lib) can pin the same contract.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from holo import cert as cert_mod

# ----------------------------------------------------------- path helpers


def test_cert_paths_layout(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    priv, pub, c, meta = cert_mod.cert_paths(key)
    assert priv == key
    assert pub.name == "host-key.pub"
    assert c.name == "host-key-cert.pub"
    assert meta.name == "host-key-cert.json"


def test_resolve_backend_explicit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOLO_BACKEND", "http://from-env:8081")
    assert (
        cert_mod.resolve_backend("http://from-flag:9999")
        == "http://from-flag:9999"
    )


def test_resolve_backend_env_wins_over_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOLO_BACKEND", "http://from-env:8081")
    assert cert_mod.resolve_backend(None) == "http://from-env:8081"


def test_resolve_backend_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOLO_BACKEND", raising=False)
    assert cert_mod.resolve_backend(None) == cert_mod.DEFAULT_BACKEND_URL


# ----------------------------------------------------- keypair generation


def _make_priv_pub(priv: Path, pub: Path) -> None:
    """Side-effect for the mock: emulate ssh-keygen writing both files."""
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n")
    pub.write_text("ssh-ed25519 AAAA-fake holo@test\n")


def test_ensure_keypair_generates_when_missing(tmp_path: Path) -> None:
    key_path = tmp_path / ".holo" / "host-key"
    pub_path = key_path.with_name("host-key.pub")

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        # Emulate ssh-keygen writing the files it was asked to.
        _make_priv_pub(key_path, pub_path)
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("holo.cert.subprocess.run", side_effect=fake_run) as run:
        priv, pub = cert_mod.ensure_keypair(key_path)
    assert priv == key_path
    assert pub == pub_path
    args, kwargs = run.call_args
    cmd = args[0]
    assert cmd[0] == "ssh-keygen"
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "ed25519"
    assert "-N" in cmd and cmd[cmd.index("-N") + 1] == ""
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == str(key_path)


def test_ensure_keypair_idempotent(tmp_path: Path) -> None:
    """If both files exist, ssh-keygen is NOT invoked."""
    key_path = tmp_path / "host-key"
    _make_priv_pub(key_path, key_path.with_name("host-key.pub"))
    with patch("holo.cert.subprocess.run") as run:
        priv, pub = cert_mod.ensure_keypair(key_path)
    run.assert_not_called()
    assert priv == key_path


def test_ensure_keypair_creates_parent_dir(tmp_path: Path) -> None:
    key_path = tmp_path / "deep" / "nested" / "host-key"
    pub_path = key_path.with_name("host-key.pub")

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        _make_priv_pub(key_path, pub_path)
        return MagicMock(returncode=0)

    with patch("holo.cert.subprocess.run", side_effect=fake_run):
        cert_mod.ensure_keypair(key_path)
    assert key_path.parent.is_dir()


def test_ensure_keypair_chmods_files(tmp_path: Path) -> None:
    key_path = tmp_path / "host-key"
    pub_path = key_path.with_name("host-key.pub")

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        _make_priv_pub(key_path, pub_path)
        return MagicMock(returncode=0)

    with patch("holo.cert.subprocess.run", side_effect=fake_run):
        cert_mod.ensure_keypair(key_path)
    # 0o600 on private key, 0o644 on public.
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert (pub_path.stat().st_mode & 0o777) == 0o644


def test_ensure_keypair_partial_state_recovers(tmp_path: Path) -> None:
    """If a prior run left the priv but no pub (or vice versa), regenerate."""
    key_path = tmp_path / "host-key"
    key_path.write_text("orphan-private")
    pub_path = key_path.with_name("host-key.pub")

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        _make_priv_pub(key_path, pub_path)
        return MagicMock(returncode=0)

    with patch("holo.cert.subprocess.run", side_effect=fake_run) as run:
        cert_mod.ensure_keypair(key_path)
    run.assert_called_once()


# ---------------------------------------------------- backend cert fetch


def _fake_urlopen_response(payload: dict[str, Any]) -> Any:
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    cm.__exit__.return_value = False
    return cm


def test_fetch_cert_posts_pubkey_and_returns_response(tmp_path: Path) -> None:
    pub_path = tmp_path / "host-key.pub"
    pub_path.write_text("ssh-ed25519 AAAA-fake holo@test")

    payload = {
        "certificate": "ssh-ed25519-cert-v01@openssh.com AAAA-cert",
        "caPublicLine": "ssh-ed25519 AAAA-ca s3r9-ssh-ca",
        "validAfter": 1700000000,
        "validBefore": 1700003600,
        "keyId": "s3r9:local-dev:1700000000",
    }

    with patch(
        "holo.cert.urllib.request.urlopen",
        return_value=_fake_urlopen_response(payload),
    ) as mock_open:
        result = cert_mod.fetch_cert(pub_path, "http://localhost:8081/")
    # Verify the URL included the trailing path; trailing slash on
    # backend should be stripped.
    req = mock_open.call_args[0][0]
    assert req.full_url == "http://localhost:8081/v1/ssh/sign"
    assert req.method == "POST"
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"publicKey": "ssh-ed25519 AAAA-fake holo@test"}
    assert result == payload


def test_fetch_cert_empty_pubkey_raises(tmp_path: Path) -> None:
    pub_path = tmp_path / "host-key.pub"
    pub_path.write_text("")
    with pytest.raises(cert_mod.CertFetchError, match="empty public key"):
        cert_mod.fetch_cert(pub_path, "http://localhost:8081")


def test_fetch_cert_http_error_wrapped(tmp_path: Path) -> None:
    import urllib.error

    pub_path = tmp_path / "host-key.pub"
    pub_path.write_text("ssh-ed25519 AAAA-fake holo@test")
    with patch(
        "holo.cert.urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "http://x", 503, "Service Unavailable", {}, None
        ),
    ):
        with pytest.raises(cert_mod.CertFetchError, match="HTTP 503"):
            cert_mod.fetch_cert(pub_path, "http://localhost:8081")


def test_fetch_cert_url_error_wrapped(tmp_path: Path) -> None:
    import urllib.error

    pub_path = tmp_path / "host-key.pub"
    pub_path.write_text("ssh-ed25519 AAAA-fake holo@test")
    with patch(
        "holo.cert.urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        with pytest.raises(cert_mod.CertFetchError, match="could not reach"):
            cert_mod.fetch_cert(pub_path, "http://localhost:8081")


def test_fetch_cert_missing_required_field_raises(tmp_path: Path) -> None:
    pub_path = tmp_path / "host-key.pub"
    pub_path.write_text("ssh-ed25519 AAAA-fake holo@test")
    # Missing validBefore should raise.
    payload = {
        "certificate": "ssh-cert-stuff",
        "validAfter": 1700000000,
    }
    with patch(
        "holo.cert.urllib.request.urlopen",
        return_value=_fake_urlopen_response(payload),
    ):
        with pytest.raises(cert_mod.CertFetchError, match="missing fields"):
            cert_mod.fetch_cert(pub_path, "http://localhost:8081")


# ---------------------------------------------------------- save / status


def test_save_cert_atomic_write(tmp_path: Path) -> None:
    cert = tmp_path / "host-key-cert.pub"
    meta = tmp_path / "host-key-cert.json"
    response = {
        "certificate": "ssh-ed25519-cert-v01@openssh.com AAAA-cert",
        "validAfter": 1700000000,
        "validBefore": 1700003600,
        "keyId": "s3r9:local-dev:1700000000",
        "caPublicLine": "ssh-ed25519 AAAA-ca s3r9-ssh-ca",
    }
    cert_mod.save_cert(response, cert, meta)
    assert (
        cert.read_text().strip()
        == "ssh-ed25519-cert-v01@openssh.com AAAA-cert"
    )
    meta_data = json.loads(meta.read_text())
    assert meta_data["validAfter"] == 1700000000
    assert meta_data["validBefore"] == 1700003600
    assert meta_data["keyId"] == "s3r9:local-dev:1700000000"
    assert "fetched_at" in meta_data


def test_cert_status_no_files(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    s = cert_mod.cert_status(key)
    assert s["has_priv"] is False
    assert s["has_cert"] is False
    assert s["has_meta"] is False


def test_cert_status_with_meta(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    priv, pub, cert, meta = cert_mod.cert_paths(key)
    priv.write_text("priv")
    pub.write_text("ssh-ed25519 AAAA-fake holo@test")
    cert.write_text("ssh-ed25519-cert-v01 AAAA-cert")
    now = int(time.time())
    meta.write_text(
        json.dumps(
            {
                "validAfter": now - 100,
                "validBefore": now + 1000,
                "keyId": "test-key-id",
                "caPublicLine": "ssh-ed25519 AAAA-ca",
                "fetched_at": now - 100,
            }
        )
    )
    s = cert_mod.cert_status(key)
    assert s["has_priv"] is True
    assert s["has_pub"] is True
    assert s["has_cert"] is True
    assert s["has_meta"] is True
    assert s["public_line"] == "ssh-ed25519 AAAA-fake holo@test"
    assert s["valid_after"] == now - 100
    assert s["valid_before"] == now + 1000
    assert s["key_id"] == "test-key-id"
    assert s["expired"] is False
    assert s["ttl_seconds"] >= 999  # within a tick of 1000


def test_cert_status_expired(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    _, _, cert, meta = cert_mod.cert_paths(key)
    cert.write_text("expired-cert")
    now = int(time.time())
    meta.write_text(
        json.dumps(
            {
                "validAfter": now - 7200,
                "validBefore": now - 3600,
                "keyId": "x",
            }
        )
    )
    s = cert_mod.cert_status(key)
    assert s["expired"] is True
    assert s["ttl_seconds"] == 0


# ----------------------------------------------------- refresh threshold


def _write_meta(key: Path, validAfter: int, validBefore: int) -> None:
    _, _, cert, meta = cert_mod.cert_paths(key)
    cert.write_text("cert-content")
    meta.write_text(json.dumps({"validAfter": validAfter, "validBefore": validBefore}))


def test_needs_refresh_when_no_files(tmp_path: Path) -> None:
    assert cert_mod.needs_refresh(tmp_path / "host-key") is True


def test_needs_refresh_when_meta_corrupt(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    _, _, cert, meta = cert_mod.cert_paths(key)
    cert.write_text("c")
    meta.write_text("not-json{{")
    assert cert_mod.needs_refresh(key) is True


def test_needs_refresh_false_when_fresh(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    now = int(time.time())
    _write_meta(key, now - 60, now + 3540)  # 60s in, 3540s left of 3600s
    assert cert_mod.needs_refresh(key, now=now) is False


def test_needs_refresh_true_when_below_threshold(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    now = int(time.time())
    # 3600s lifetime, only 700s remaining = 19% < 25%.
    _write_meta(key, now - 2900, now + 700)
    assert cert_mod.needs_refresh(key, now=now) is True


def test_needs_refresh_true_when_expired(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    now = int(time.time())
    _write_meta(key, now - 7200, now - 3600)
    assert cert_mod.needs_refresh(key, now=now) is True


def test_needs_refresh_threshold_override(tmp_path: Path) -> None:
    key = tmp_path / "host-key"
    now = int(time.time())
    _write_meta(key, now - 1800, now + 1800)  # exactly 50% remaining
    # At threshold=0.10, 50% remaining is fine.
    assert cert_mod.needs_refresh(key, now=now, threshold=0.10) is False
    # At threshold=0.75, 50% remaining is too low.
    assert cert_mod.needs_refresh(key, now=now, threshold=0.75) is True


# ---------------------------------------------------------- orchestration


def test_get_or_refresh_skips_when_fresh(tmp_path: Path) -> None:
    """A fresh cert on disk is reused; no ssh-keygen, no /v1/ssh/sign."""
    key = tmp_path / "host-key"
    priv, pub, cert, meta = cert_mod.cert_paths(key)
    priv.write_text("p")
    pub.write_text("ssh-ed25519 AAAA-fake holo@test")
    cert.write_text("c")
    now = int(time.time())
    meta.write_text(json.dumps({"validAfter": now - 60, "validBefore": now + 3540}))

    with patch("holo.cert.subprocess.run") as run, patch(
        "holo.cert.urllib.request.urlopen"
    ) as urlopen:
        cert_mod.get_or_refresh(backend="http://localhost:8081", key_path=key)
    run.assert_not_called()
    urlopen.assert_not_called()


def test_get_or_refresh_force_refetches(tmp_path: Path) -> None:
    """`force=True` skips the freshness check."""
    key = tmp_path / "host-key"
    priv, pub, cert, meta = cert_mod.cert_paths(key)
    priv.write_text("p")
    pub.write_text("ssh-ed25519 AAAA-fake holo@test")
    cert.write_text("c-old")
    now = int(time.time())
    meta.write_text(json.dumps({"validAfter": now - 60, "validBefore": now + 3540}))

    payload = {
        "certificate": "ssh-ed25519-cert-v01 AAAA-NEW",
        "validAfter": now,
        "validBefore": now + 3600,
        "keyId": "new",
    }
    with patch(
        "holo.cert.urllib.request.urlopen",
        return_value=_fake_urlopen_response(payload),
    ):
        cert_mod.get_or_refresh(
            backend="http://localhost:8081", key_path=key, force=True
        )
    assert cert.read_text().strip() == "ssh-ed25519-cert-v01 AAAA-NEW"


def test_get_or_refresh_full_first_run(tmp_path: Path) -> None:
    """No keypair, no cert: generate keypair, fetch cert, save both."""
    key = tmp_path / ".holo" / "host-key"
    pub = key.with_name("host-key.pub")

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        _make_priv_pub(key, pub)
        return MagicMock(returncode=0)

    payload = {
        "certificate": "ssh-ed25519-cert-v01 AAAA-FRESH",
        "validAfter": 1700000000,
        "validBefore": 1700003600,
        "keyId": "fresh",
        "caPublicLine": "ssh-ed25519 AAAA-ca",
    }
    with patch(
        "holo.cert.subprocess.run", side_effect=fake_run
    ) as run, patch(
        "holo.cert.urllib.request.urlopen",
        return_value=_fake_urlopen_response(payload),
    ) as urlopen:
        status = cert_mod.get_or_refresh(
            backend="http://localhost:8081", key_path=key
        )
    run.assert_called_once()  # ssh-keygen
    urlopen.assert_called_once()  # /v1/ssh/sign
    assert status["has_cert"] is True
    assert status["has_meta"] is True
    assert status["valid_before"] == 1700003600
    _, _, cert, _ = cert_mod.cert_paths(key)
    assert cert.read_text().strip() == "ssh-ed25519-cert-v01 AAAA-FRESH"


def test_get_or_refresh_uses_resolved_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When --backend isn't passed, falls back to env var, then default."""
    monkeypatch.setenv("HOLO_BACKEND", "http://from-env:8081")
    key = tmp_path / "host-key"
    pub = key.with_name("host-key.pub")

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        _make_priv_pub(key, pub)
        return MagicMock(returncode=0)

    payload = {
        "certificate": "c",
        "validAfter": 1700000000,
        "validBefore": 1700003600,
    }
    with patch("holo.cert.subprocess.run", side_effect=fake_run), patch(
        "holo.cert.urllib.request.urlopen",
        return_value=_fake_urlopen_response(payload),
    ) as urlopen:
        cert_mod.get_or_refresh(backend=None, key_path=key)
    req = urlopen.call_args[0][0]
    assert req.full_url == "http://from-env:8081/v1/ssh/sign"


def test_get_or_refresh_propagates_keygen_failure(
    tmp_path: Path,
) -> None:
    """ssh-keygen failure surfaces as CalledProcessError to the caller."""
    key = tmp_path / "host-key"
    with patch(
        "holo.cert.subprocess.run",
        side_effect=subprocess.CalledProcessError(
            1, ["ssh-keygen"], output=b"", stderr=b"disk full"
        ),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            cert_mod.get_or_refresh(
                backend="http://localhost:8081", key_path=key
            )
