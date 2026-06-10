"""mDNS / DNS-SD announcer for CloudCity hosts.

Broadcasts a `_cloudcity._tcp.local.` service so holo daemons on the
LAN can discover machines running c2w-net (the Docker container in the
desktop repo that provides the c2w VM with delegate networking and an
SSH endpoint). Holo daemons use the announcement to set up reverse
SSH tunnels into the CloudCity, which is what enables the desktop
SPA's click-to-launch flow to reach tmux hosts that the c2w-net
container can't address directly (Docker Desktop on macOS doesn't
forward container traffic to LAN peers).

Spec:
  https://github.com/bradclarkalexander/desktop/blob/develop/docs/holo-cloudcity-tunnel-spec.md

This module announces metadata about a CloudCity *host* (where
c2w-net's sshd is reachable, and optionally where to fetch certs that
c2w-net trusts). It does NOT announce session or user state — that's
the existing `_holo-session._tcp.local.` announcer's responsibility
in `holo.announce`.

Usage (foreground process):

    a = CloudCityAnnouncer(port=2222, backend="http://localhost:8081")
    a.start()
    try:
        signal.pause()  # or whatever holds the process open
    finally:
        a.stop()
"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import json
import logging
import socket
import urllib.error
import urllib.request
import uuid
from typing import TYPE_CHECKING

# Reuse the IP-enumeration helpers from announce.py rather than
# duplicating them. They're module-private over there but stable —
# `_resolve_ip_overrides` in particular has the prefix-filter
# semantics (e.g. "192.168.1.") we want to support symmetrically.
from holo.announce import (
    _enumerate_local_ipv4,
    _is_usable_ipv4,
    _resolve_ip_overrides,
)

if TYPE_CHECKING:
    from zeroconf import ServiceInfo, Zeroconf

SERVICE_TYPE = "_cloudcity._tcp.local."
TXT_SCHEMA_VERSION = "1"

# TXT field names — single source of truth shared with the eventual
# CloudCity discoverer (Phase 2 of the spec). Spec §3.2.
FIELD_V = "v"
FIELD_HOST = "host"
FIELD_IPS = "ips"
FIELD_PORT = "port"
FIELD_BACKEND = "backend"
FIELD_CA_FPS = "ca_fps"
FIELD_USER = "user"
FIELD_VERSION = "version"

# A TXT missing any of these is malformed and should be dropped by
# discoverers.
REQUIRED_FIELDS: tuple[str, ...] = (
    FIELD_V,
    FIELD_HOST,
    FIELD_IPS,
    FIELD_PORT,
)

# Fields parsed/emitted as integers in the JSON contract. TXT carries
# them as UTF-8 strings; a future discoverer should convert.
INT_FIELDS: frozenset[str] = frozenset({FIELD_PORT})

DEFAULT_PORT = 2222
DEFAULT_BACKEND_URL = "http://localhost:8081"

# Used by `--backend auto` to probe whether a local backend exists.
# Short timeout — we'd rather skip the backend field than block startup
# on a stalled probe.
_BACKEND_PROBE_TIMEOUT_S = 1.5

_log = logging.getLogger(__name__)


class CloudCityAnnouncer:
    """Lifecycle wrapper around a single CloudCity service registration.

    Construct with the CloudCity host's exposed sshd port + optional
    backend metadata, call ``start()`` to register, ``stop()`` to
    unregister cleanly. Both calls are idempotent.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        ips: list[str] | None = None,
        backend: str | None = None,
        ca_fps: list[str] | None = None,
        user: str | None = None,
        version: str | None = None,
        instance: str | None = None,
    ) -> None:
        self.port = port
        self.ips_override = ips
        self.backend = backend
        self.ca_fps_override = ca_fps
        self.user = user or getpass.getuser()
        self.version = version
        self.instance_override = instance
        self._zeroconf: Zeroconf | None = None
        self._service_info: ServiceInfo | None = None

    def build_properties(self) -> dict[bytes, bytes]:
        """Assemble the TXT record.

        Required fields are always emitted. Optional fields (backend,
        ca_fps, user, version) are omitted when not specified, keeping
        the record compact and letting consumers distinguish "unset"
        from "set to empty".
        """
        props: dict[bytes, bytes] = {}

        def put(key: str, value: str | None) -> None:
            if value is None or value == "":
                return
            props[key.encode("utf-8")] = value.encode("utf-8")

        put(FIELD_V, TXT_SCHEMA_VERSION)
        put(FIELD_HOST, socket.gethostname())
        ips = self._collect_ips()
        if ips:
            put(FIELD_IPS, ",".join(ips))
        put(FIELD_PORT, str(self.port))
        put(FIELD_BACKEND, self.backend)

        ca_fps = self._collect_ca_fps()
        if ca_fps:
            put(FIELD_CA_FPS, ",".join(ca_fps))

        put(FIELD_USER, self.user)
        put(FIELD_VERSION, self.version)

        return props

    def _collect_ips(self) -> list[str]:
        """Pick IPv4 addresses to advertise.

        Same precedence as the holo-session announcer:
        1. Explicit ``ips=`` constructor override (literal IPs and/or
           trailing-dot prefix filters per ``_resolve_ip_overrides``).
        2. Auto-enumerate every interface, drop loopback / link-local.
        3. Fall back to ``gethostbyname`` so we never advertise an
           empty A-record set.
        """
        if self.ips_override:
            resolved = _resolve_ip_overrides(self.ips_override)
            return [ip for ip in resolved if _is_usable_ipv4(ip)]
        enumerated = _enumerate_local_ipv4()
        if enumerated:
            return enumerated
        try:
            return [socket.gethostbyname(socket.gethostname())]
        except OSError:
            return []

    def _collect_ca_fps(self) -> list[str]:
        """Resolve the ``ca_fps`` field.

        - Explicit override wins (each entry advertised verbatim).
        - Otherwise, if a ``backend`` is set, fetch
          ``<backend>/v1/ssh/ca`` and compute the OpenSSH SHA256
          fingerprint of the returned pubkey.
        - On any failure (backend unreachable, malformed response),
          return ``[]``. The field is optional; omitting it is
          preferable to advertising stale/wrong fingerprints.
        """
        if self.ca_fps_override is not None:
            return list(self.ca_fps_override)
        if not self.backend:
            return []

        url = self.backend.rstrip("/") + "/v1/ssh/ca"
        try:
            with urllib.request.urlopen(
                url, timeout=_BACKEND_PROBE_TIMEOUT_S
            ) as resp:
                body = resp.read()
        except (urllib.error.URLError, OSError, ValueError):
            _log.warning(
                "ca_fps auto-probe: failed to reach %s", url
            )
            return []

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            _log.warning("ca_fps auto-probe: %s returned non-JSON", url)
            return []

        pubkey_line = data.get("caPublicLine") if isinstance(data, dict) else None
        if not isinstance(pubkey_line, str) or not pubkey_line:
            _log.warning(
                "ca_fps auto-probe: %s returned no caPublicLine", url
            )
            return []

        fp = ssh_pubkey_fingerprint(pubkey_line)
        return [fp] if fp else []

    def start(self) -> None:
        """Register the service with the local zeroconf stack.

        Opens a multicast socket on 224.0.0.251:5353. ``allow_name_change=True``
        lets the stack rename the instance on collision (``-2``, ``-3``…)
        — but we already salt the instance name with a UUID, so renames
        are unlikely.
        """
        from zeroconf import IPVersion, ServiceInfo, Zeroconf

        if self._zeroconf is not None:
            return

        instance = self.instance_override or self._instance_name()
        properties = self.build_properties()
        addresses = [
            socket.inet_aton(ip) for ip in self._collect_ips()
        ] or [socket.inet_aton("127.0.0.1")]

        self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        self._service_info = ServiceInfo(
            type_=SERVICE_TYPE,
            name=f"{instance}.{SERVICE_TYPE}",
            addresses=addresses,
            port=self.port,
            properties=properties,
            server=f"{socket.gethostname().split('.')[0]}.local.",
        )
        self._zeroconf.register_service(
            self._service_info, allow_name_change=True
        )

    def stop(self) -> None:
        if self._zeroconf is None:
            return
        try:
            if self._service_info is not None:
                self._zeroconf.unregister_service(self._service_info)
        except Exception:
            _log.exception("zeroconf unregister failed")
        try:
            self._zeroconf.close()
        except Exception:
            _log.exception("zeroconf close failed")
        finally:
            self._zeroconf = None
            self._service_info = None

    def _instance_name(self) -> str:
        # DNS label cap is 63 bytes (RFC 1035). Salt with 6 hex chars
        # so distinct CloudCity processes on the same hostname don't
        # collide on instance label.
        salt = uuid.uuid4().hex[:6]
        suffix = f"-{salt}"
        budget = 63 - len(suffix)
        body = f"cloudcity-{socket.gethostname().split('.')[0]}"
        return body[:budget] + suffix


def ssh_pubkey_fingerprint(pubkey_line: str) -> str | None:
    """Compute the OpenSSH-style SHA256 fingerprint of a public key.

    Format matches ``ssh-keygen -l -f``: ``SHA256:<base64-no-padding>``.
    Returns ``None`` on any parse error — caller decides whether to
    surface the failure or silently omit the fingerprint.
    """
    parts = pubkey_line.strip().split()
    if len(parts) < 2:
        return None
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error):
        return None
    digest = hashlib.sha256(raw).digest()
    return "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode("ascii")


__all__ = [
    "DEFAULT_BACKEND_URL",
    "DEFAULT_PORT",
    "FIELD_BACKEND",
    "FIELD_CA_FPS",
    "FIELD_HOST",
    "FIELD_IPS",
    "FIELD_PORT",
    "FIELD_USER",
    "FIELD_V",
    "FIELD_VERSION",
    "CloudCityAnnouncer",
    "INT_FIELDS",
    "REQUIRED_FIELDS",
    "SERVICE_TYPE",
    "TXT_SCHEMA_VERSION",
    "ssh_pubkey_fingerprint",
]
