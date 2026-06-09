"""Holo daemon's outbound-SSH cert (Phase 3 of the CloudCity tunnel spec).

The reverse-tunnel architecture has the holo daemon SSH out to a
CloudCity host's exposed sshd as ``lando``. C2w-net's
``TrustedUserCAKeys`` accepts certs signed by any of the bundled CAs,
so the daemon needs to fetch a short-lived cert from the s3r9 backend
on a timer rather than enrolling its pubkey per-c2w-net.

This module owns the on-disk identity and the refresh dance:

    ~/.holo/
      host-key             - Ed25519 private key (chmod 600)
      host-key.pub         - matching pubkey
      host-key-cert.pub    - cert signed by the backend's CA; OpenSSH
                             auto-presents this when ssh sees the
                             matching private key, no agent gymnastics
      host-key-cert.json   - the rest of /v1/ssh/sign's response (
                             validAfter, validBefore, keyId,
                             caPublicLine, fetched_at). Read by
                             ``cert_status`` and ``needs_refresh`` so
                             we don't have to parse the OpenSSH cert
                             wire format.

Spec:
  https://github.com/bradclarkalexander/desktop/blob/develop/docs/holo-cloudcity-tunnel-spec.md §4.3

Backend selection precedence (used by ``resolve_backend``):
  1. explicit ``backend`` argument (CLI ``--backend URL``)
  2. ``HOLO_BACKEND`` env var
  3. (future) CloudCity TXT field — wired in by Phase 4
  4. ``DEFAULT_BACKEND_URL``

Auth:
  In ``LOCAL_DEV_MODE=1`` the backend's ``/v1/ssh/sign`` requires no
  auth, so this module sends an unauthenticated POST. Production-bound
  deployments need device-flow tokens (deferred — see the spec's
  §6.1). The request builder is small enough that adding an auth
  header later is a one-line change.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


# Default location for holo daemon's persistent identity. ``~/.holo`` is
# created with mode 0700 on first use.
DEFAULT_KEY_DIR = Path.home() / ".holo"
DEFAULT_KEY_PATH = DEFAULT_KEY_DIR / "host-key"

# Default backend URL when neither --backend nor HOLO_BACKEND is set.
# Mirrors the spec's §4.3 fallback. Local-dev callers should override.
DEFAULT_BACKEND_URL = "https://api-dev.tai.sh"

# When the cert has less than this fraction of its lifetime remaining,
# ``needs_refresh`` returns True. 25% matches the desktop SPA's
# c2w-init.js refresh threshold so the two clients age in step.
REFRESH_THRESHOLD = 0.25

# How long /v1/ssh/sign is given to respond. Chosen long enough to
# tolerate slow first-connect TLS handshakes but short enough that a
# wedged backend doesn't block the whole tunnel-bring-up flow.
_SIGN_TIMEOUT_S = 10.0

# Default Origin header sent on cert-fetch requests.
#
# The s3r9 backend has a Phase-4e CSRF guard that rejects state-
# changing methods (POST/PUT/PATCH/DELETE) when the Origin header is
# missing or not in its CORS allowlist. That guard makes sense for
# browser callers (where ambient cookies create a CSRF surface) but
# is a false positive for a CLI tool with no ambient credentials.
# Until the backend grows a "skip CSRF when there's no cookie" branch
# (deferred to the device-flow auth design), holo daemons send
# ``http://localhost:8888`` — the first entry in the backend's
# LOCAL_DEV_MODE / dev allowlist. Override with ``HOLO_ORIGIN`` when
# pointing holo at a backend whose allowlist doesn't include it.
DEFAULT_ORIGIN = "http://localhost:8888"


# ----------------------------------------------------------- path helpers


def cert_paths(key_path: Path) -> tuple[Path, Path, Path, Path]:
    """Map a private-key path to its companion files.

    Returns ``(priv, pub, cert, meta)``. The ``-cert.pub`` suffix is
    the convention OpenSSH expects for automatic cert presentation
    (``ssh`` looks for ``<key>-cert.pub`` next to ``<key>``).
    """
    priv = key_path
    pub = priv.with_name(priv.name + ".pub")
    cert = priv.with_name(priv.name + "-cert.pub")
    meta = priv.with_name(priv.name + "-cert.json")
    return priv, pub, cert, meta


def resolve_backend(explicit: str | None = None) -> str:
    """Pick the backend URL using the spec's precedence order."""
    if explicit:
        return explicit
    env = os.environ.get("HOLO_BACKEND")
    if env:
        return env
    return DEFAULT_BACKEND_URL


# ----------------------------------------------------- keypair generation


def ensure_keypair(
    key_path: Path = DEFAULT_KEY_PATH,
    *,
    comment: str | None = None,
) -> tuple[Path, Path]:
    """Generate an Ed25519 keypair at ``key_path`` if missing.

    Returns ``(priv, pub)``. Idempotent — if the files already exist
    the existing keypair is reused (the cert chain depends on a stable
    pubkey across refreshes).

    Shells out to ``ssh-keygen`` rather than depending on the
    ``cryptography`` library: ssh-keygen is universally available on
    macOS / Linux, writes files in exactly the format OpenSSH expects,
    and saves us a heavy dependency.
    """
    priv, pub, _, _ = cert_paths(key_path)
    if priv.exists() and pub.exists():
        return priv, pub
    if comment is None:
        comment = f"holo@{socket.gethostname().split('.')[0]}"
    priv.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # ssh-keygen refuses to overwrite — if we got here with one of the
    # two files missing, clear both so the regen is clean.
    if priv.exists():
        priv.unlink()
    if pub.exists():
        pub.unlink()
    subprocess.run(
        [
            "ssh-keygen",
            "-t", "ed25519",
            "-N", "",
            "-C", comment,
            "-f", str(priv),
        ],
        check=True,
        capture_output=True,
    )
    # ssh-keygen sets 0600 on the private key already, but be defensive
    # in case a future ssh-keygen rev changes that default.
    priv.chmod(0o600)
    pub.chmod(0o644)
    return priv, pub


# ---------------------------------------------------- backend cert fetch


class CertFetchError(Exception):
    """Raised when /v1/ssh/sign rejects, errors, or returns a malformed response."""


def fetch_cert(
    pub_path: Path,
    backend: str,
    *,
    timeout: float = _SIGN_TIMEOUT_S,
    origin: str | None = None,
) -> dict[str, Any]:
    """POST the pubkey to ``<backend>/v1/ssh/sign`` and return the response.

    Response shape (matches s3r9-backend's ``/v1/ssh/sign``):
        certificate   — OpenSSH-format cert line
        caPublicLine  — CA pubkey that signed it
        validAfter    — int unix epoch seconds
        validBefore   — int unix epoch seconds
        keyId         — backend-assigned audit label

    ``origin`` is sent as the HTTP ``Origin`` header to satisfy the
    backend's CSRF guard (see ``DEFAULT_ORIGIN`` for context). Pass
    explicitly when targeting a backend whose CORS allowlist doesn't
    include the default; ``HOLO_ORIGIN`` env var also overrides.
    """
    pub_line = pub_path.read_text().strip()
    if not pub_line:
        raise CertFetchError(f"empty public key at {pub_path}")
    body = json.dumps({"publicKey": pub_line}).encode("utf-8")
    url = backend.rstrip("/") + "/v1/ssh/sign"
    resolved_origin = origin or os.environ.get("HOLO_ORIGIN") or DEFAULT_ORIGIN
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Origin": resolved_origin,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise CertFetchError(
            f"backend rejected /v1/ssh/sign: HTTP {e.code} from {url}"
        ) from e
    except (urllib.error.URLError, OSError) as e:
        raise CertFetchError(
            f"could not reach backend /v1/ssh/sign at {url}: {e}"
        ) from e
    except json.JSONDecodeError as e:
        raise CertFetchError(
            f"backend /v1/ssh/sign at {url} returned non-JSON"
        ) from e

    required = ("certificate", "validAfter", "validBefore")
    missing = [k for k in required if k not in data]
    if missing:
        raise CertFetchError(
            f"backend /v1/ssh/sign response missing fields: {missing}"
        )
    return data


# ----------------------------------------------------------- on-disk state


def save_cert(
    response: dict[str, Any],
    cert_path: Path,
    meta_path: Path,
) -> None:
    """Write the cert + sidecar metadata atomically.

    The metadata is what we use later to decide whether the cert is
    still fresh (``needs_refresh``) without having to parse the
    OpenSSH cert wire format.
    """
    cert_text = (response["certificate"] or "").strip() + "\n"
    meta = {
        "validAfter": response["validAfter"],
        "validBefore": response["validBefore"],
        "keyId": response.get("keyId"),
        "caPublicLine": response.get("caPublicLine"),
        "fetched_at": int(time.time()),
    }
    # Atomic-ish: write to a tmp file alongside, then rename.
    cert_tmp = cert_path.with_suffix(cert_path.suffix + ".tmp")
    meta_tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    cert_tmp.write_text(cert_text)
    cert_tmp.chmod(0o644)
    cert_tmp.replace(cert_path)
    meta_tmp.write_text(json.dumps(meta, indent=2) + "\n")
    meta_tmp.chmod(0o644)
    meta_tmp.replace(meta_path)


def cert_status(
    key_path: Path = DEFAULT_KEY_PATH,
) -> dict[str, Any]:
    """Inspect on-disk state without touching the network.

    Returns a dict with all observable booleans + the meta fields when
    available. Caller can use this for ``holo cert show`` output and
    for ``needs_refresh`` decisions.
    """
    priv, pub, cert, meta = cert_paths(key_path)
    out: dict[str, Any] = {
        "key_path": str(priv),
        "has_priv": priv.exists(),
        "has_pub": pub.exists(),
        "has_cert": cert.exists(),
        "has_meta": meta.exists(),
    }
    if pub.exists():
        try:
            out["public_line"] = pub.read_text().strip()
        except OSError as e:
            _log.warning("cert_status: failed to read %s: %s", pub, e)
    if meta.exists():
        try:
            data = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError) as e:
            _log.warning("cert_status: bad meta at %s: %s", meta, e)
        else:
            out.update(
                {
                    "valid_after": data.get("validAfter"),
                    "valid_before": data.get("validBefore"),
                    "key_id": data.get("keyId"),
                    "ca_public_line": data.get("caPublicLine"),
                    "fetched_at": data.get("fetched_at"),
                }
            )
            now = int(time.time())
            valid_before = data.get("validBefore")
            if isinstance(valid_before, int):
                out["expired"] = now >= valid_before
                out["ttl_seconds"] = max(0, valid_before - now)
    return out


def needs_refresh(
    key_path: Path = DEFAULT_KEY_PATH,
    *,
    threshold: float = REFRESH_THRESHOLD,
    now: int | None = None,
) -> bool:
    """True if a cert refresh is needed.

    Refresh when any of: no cert on disk, no meta on disk, expired,
    or remaining TTL < ``threshold`` of the original lifetime.
    """
    if now is None:
        now = int(time.time())
    _, _, cert, meta = cert_paths(key_path)
    if not cert.exists() or not meta.exists():
        return True
    try:
        data = json.loads(meta.read_text())
    except (OSError, json.JSONDecodeError):
        return True
    valid_after = data.get("validAfter")
    valid_before = data.get("validBefore")
    if not isinstance(valid_after, int) or not isinstance(valid_before, int):
        return True
    if now >= valid_before:
        return True
    lifetime = valid_before - valid_after
    if lifetime <= 0:
        return True
    remaining = valid_before - now
    return (remaining / lifetime) < threshold


# ------------------------------------------------------------ orchestration


def get_or_refresh(
    *,
    backend: str | None = None,
    key_path: Path = DEFAULT_KEY_PATH,
    force: bool = False,
) -> dict[str, Any]:
    """End-to-end: ensure keypair, fetch cert if needed, save, return status.

    ``force=True`` skips the ``needs_refresh`` check and re-fetches
    unconditionally — what ``holo cert refresh`` calls.
    """
    priv, pub = ensure_keypair(key_path)
    if not force and not needs_refresh(key_path):
        return cert_status(key_path)
    resolved_backend = resolve_backend(backend)
    response = fetch_cert(pub, resolved_backend)
    _, _, cert, meta = cert_paths(key_path)
    save_cert(response, cert, meta)
    return cert_status(key_path)


__all__ = [
    "DEFAULT_BACKEND_URL",
    "DEFAULT_KEY_DIR",
    "DEFAULT_KEY_PATH",
    "REFRESH_THRESHOLD",
    "CertFetchError",
    "cert_paths",
    "cert_status",
    "ensure_keypair",
    "fetch_cert",
    "get_or_refresh",
    "needs_refresh",
    "resolve_backend",
    "save_cert",
]
