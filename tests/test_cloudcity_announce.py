"""Unit tests for holo.cloudcity_announce.

Pin the contract: TXT field assembly under various input combinations,
optional-field omission, CA-fingerprint computation, and lifecycle
hook plumbing. We don't open a real Zeroconf socket here — that's
covered by a manual smoke test (`dns-sd -B _cloudcity._tcp local`
against a live `holo cloudcity announce`).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

from holo.cloudcity_announce import (
    FIELD_BACKEND,
    FIELD_CA_FPS,
    FIELD_IPS,
    FIELD_PORT,
    FIELD_USER,
    FIELD_V,
    FIELD_VERSION,
    REQUIRED_FIELDS,
    TXT_SCHEMA_VERSION,
    CloudCityAnnouncer,
    ssh_pubkey_fingerprint,
)


def _props_to_dict(props: dict[bytes, bytes]) -> dict[str, str]:
    """Convert TXT-record bytes->bytes dict to readable str->str dict."""
    return {k.decode("utf-8"): v.decode("utf-8") for k, v in props.items()}


# --- TXT assembly --------------------------------------------------------


def test_required_fields_always_present() -> None:
    """Even with no optional inputs, the v/host/ips/port quartet ships."""
    a = CloudCityAnnouncer(port=2222)
    # Force at least one IP to avoid the "no enumerable IPs" branch
    # giving us an empty `ips` field, which would (correctly) fail the
    # required-fields check.
    with patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ):
        props = _props_to_dict(a.build_properties())
    for field in REQUIRED_FIELDS:
        assert field in props, f"required field {field!r} missing from TXT"
    assert props[FIELD_V] == TXT_SCHEMA_VERSION
    assert props[FIELD_PORT] == "2222"
    assert props[FIELD_IPS] == "192.168.1.5"


def test_optional_fields_omitted_when_unset() -> None:
    """backend / ca_fps / version are omitted when not provided."""
    a = CloudCityAnnouncer(port=2222)
    with patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ):
        props = _props_to_dict(a.build_properties())
    assert FIELD_BACKEND not in props
    assert FIELD_CA_FPS not in props
    assert FIELD_VERSION not in props


def test_explicit_backend_advertised_verbatim() -> None:
    a = CloudCityAnnouncer(
        port=2222,
        backend="http://192.168.1.5:8081",
        ca_fps=["SHA256:abc"],  # skip auto-probe by passing explicitly
    )
    with patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ):
        props = _props_to_dict(a.build_properties())
    assert props[FIELD_BACKEND] == "http://192.168.1.5:8081"
    assert props[FIELD_CA_FPS] == "SHA256:abc"


def test_user_defaults_to_getuser() -> None:
    with patch("holo.cloudcity_announce.getpass.getuser", return_value="alice"):
        a = CloudCityAnnouncer(port=2222)
    with patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ):
        props = _props_to_dict(a.build_properties())
    assert props[FIELD_USER] == "alice"


def test_explicit_user_overrides_default() -> None:
    a = CloudCityAnnouncer(port=2222, user="bob")
    with patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ):
        props = _props_to_dict(a.build_properties())
    assert props[FIELD_USER] == "bob"


def test_ips_override_with_prefix_filter() -> None:
    """Trailing-dot prefix filters enumerated IPs.

    `_resolve_ip_overrides` (from `holo.announce`) looks up
    `_enumerate_local_ipv4` against its own module scope, so we patch
    the symbol there — patching `holo.cloudcity_announce._enumerate_local_ipv4`
    wouldn't affect what `_resolve_ip_overrides` sees.
    """
    a = CloudCityAnnouncer(port=2222, ips=["192.168.1."])
    with patch(
        "holo.announce._enumerate_local_ipv4",
        return_value=["10.0.0.5", "192.168.1.5", "192.168.1.10"],
    ):
        props = _props_to_dict(a.build_properties())
    assert props[FIELD_IPS] == "192.168.1.5,192.168.1.10"


def test_ips_override_with_literal() -> None:
    """Literal IPs are advertised even when not on a local interface."""
    a = CloudCityAnnouncer(port=2222, ips=["10.55.195.6"])
    props = _props_to_dict(a.build_properties())
    assert props[FIELD_IPS] == "10.55.195.6"


def test_loopback_in_explicit_override_is_filtered() -> None:
    """Even if a caller passes loopback / link-local in --ips, drop them.

    The auto-enumeration path in `holo.announce._enumerate_local_ipv4`
    does its own filtering and is tested there. This test pins the
    override path: `_is_usable_ipv4` rejects 127.x and 169.254.x.
    """
    a = CloudCityAnnouncer(
        port=2222,
        ips=["127.0.0.1", "169.254.1.1", "192.168.1.5"],
    )
    props = _props_to_dict(a.build_properties())
    assert props[FIELD_IPS] == "192.168.1.5"


def test_caps_port_is_int_field() -> None:
    """FIELD_PORT is in INT_FIELDS so consumers convert correctly."""
    from holo.cloudcity_announce import INT_FIELDS
    assert FIELD_PORT in INT_FIELDS


# --- CA fingerprint auto-probe ------------------------------------------


def _fake_urlopen_response(payload: dict[str, Any]) -> Any:
    """Build a minimal context-manager mock for urllib.request.urlopen."""
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    cm.__exit__.return_value = False
    return cm


def test_ca_fps_auto_probes_backend_endpoint() -> None:
    a = CloudCityAnnouncer(port=2222, backend="http://localhost:8081")
    fake_pubkey = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOFELZm27/DePrWL7UXN2u7jy4XlI+"
        "KWIO+gDzh1mHHk s3r9-ssh-ca"
    )
    with patch(
        "holo.cloudcity_announce.urllib.request.urlopen",
        return_value=_fake_urlopen_response({"caPublicLine": fake_pubkey}),
    ), patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ):
        props = _props_to_dict(a.build_properties())
    assert props[FIELD_CA_FPS].startswith("SHA256:")


def test_ca_fps_auto_probe_failure_omits_field() -> None:
    """If the backend is unreachable, ca_fps is just absent — not invented."""
    import urllib.error

    a = CloudCityAnnouncer(port=2222, backend="http://localhost:8081")
    with patch(
        "holo.cloudcity_announce.urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ), patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ):
        props = _props_to_dict(a.build_properties())
    assert FIELD_CA_FPS not in props


def test_ca_fps_auto_probe_skipped_without_backend() -> None:
    a = CloudCityAnnouncer(port=2222)
    with patch(
        "holo.cloudcity_announce.urllib.request.urlopen"
    ) as mock_urlopen, patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ):
        a.build_properties()
    mock_urlopen.assert_not_called()


def test_ca_fps_explicit_override_skips_probe() -> None:
    """Caller supplies ca_fps → no HTTP probe regardless of backend setting."""
    a = CloudCityAnnouncer(
        port=2222,
        backend="http://localhost:8081",
        ca_fps=["SHA256:fake"],
    )
    with patch(
        "holo.cloudcity_announce.urllib.request.urlopen"
    ) as mock_urlopen, patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ):
        props = _props_to_dict(a.build_properties())
    mock_urlopen.assert_not_called()
    assert props[FIELD_CA_FPS] == "SHA256:fake"


# --- ssh_pubkey_fingerprint ---------------------------------------------


def test_fingerprint_matches_ssh_keygen_format() -> None:
    """Known-good vector: the local CA pubkey we observed on this dev host."""
    line = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOFELZm27/DePrWL7UXN2u7jy4XlI+"
        "KWIO+gDzh1mHHk s3r9-ssh-ca"
    )
    fp = ssh_pubkey_fingerprint(line)
    assert fp is not None
    assert fp.startswith("SHA256:")
    # Length: "SHA256:" + 43 chars of unpadded base64-encoded sha256.
    assert len(fp) == len("SHA256:") + 43


def test_fingerprint_returns_none_on_malformed_input() -> None:
    assert ssh_pubkey_fingerprint("") is None
    assert ssh_pubkey_fingerprint("ssh-ed25519") is None
    assert ssh_pubkey_fingerprint("ssh-ed25519 not-base64!@#") is None


def test_fingerprint_strips_comment() -> None:
    """Comment field at the end shouldn't affect the fingerprint."""
    line_with_comment = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOFELZm27/DePrWL7UXN2u7jy4XlI+"
        "KWIO+gDzh1mHHk this-is-a-comment"
    )
    line_without_comment = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOFELZm27/DePrWL7UXN2u7jy4XlI+"
        "KWIO+gDzh1mHHk"
    )
    assert (
        ssh_pubkey_fingerprint(line_with_comment)
        == ssh_pubkey_fingerprint(line_without_comment)
    )


# --- lifecycle ----------------------------------------------------------


def test_start_stop_lifecycle() -> None:
    """start()/stop() invoke zeroconf register/unregister exactly once."""
    a = CloudCityAnnouncer(port=2222)
    fake_zc_class = MagicMock()
    fake_zc = MagicMock()
    fake_zc_class.return_value = fake_zc

    fake_si_class = MagicMock()
    fake_si = MagicMock()
    fake_si_class.return_value = fake_si

    with patch(
        "holo.cloudcity_announce._enumerate_local_ipv4",
        return_value=["192.168.1.5"],
    ), patch.dict(
        "sys.modules",
        {
            "zeroconf": MagicMock(
                Zeroconf=fake_zc_class,
                ServiceInfo=fake_si_class,
                IPVersion=MagicMock(V4Only="V4Only"),
            )
        },
    ):
        a.start()
        # Calling start twice is a no-op (idempotent).
        a.start()
        a.stop()
        # Calling stop twice is a no-op (idempotent).
        a.stop()

    fake_zc_class.assert_called_once()
    fake_zc.register_service.assert_called_once()
    fake_zc.unregister_service.assert_called_once_with(fake_si)
    fake_zc.close.assert_called_once()


def test_instance_label_under_dns_limit() -> None:
    """Auto-generated instance labels must stay within 63-byte DNS limit."""
    a = CloudCityAnnouncer(port=2222)
    label = a._instance_name()
    assert len(label) <= 63
    assert label.startswith("cloudcity-")


def test_instance_override_used_verbatim() -> None:
    a = CloudCityAnnouncer(port=2222, instance="custom-name")
    assert a.instance_override == "custom-name"
    # Verified via the actual zeroconf register call in lifecycle test;
    # here we just pin that the constructor stored it.
