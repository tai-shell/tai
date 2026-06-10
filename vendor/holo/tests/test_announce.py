"""Unit tests for holo.announce.

Exercise the TXT-field assembly logic, the omit-when-not-specified
rule, tmux env detection, and the HoloMCPServer announcer plumbing.

We don't open a real Zeroconf socket here — that's covered by a
manual smoke test (`dns-sd -B _holo-session._tcp local` against a
live `holo mcp --announce`). These tests pin the contract: what the
TXT record contains given a set of inputs, and that the lifecycle
hooks are called.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from holo import __version__
from holo.announce import (
    SERVICE_TYPE,
    TXT_SCHEMA_VERSION,
    HoloAnnouncer,
    _enumerate_local_ipv4,
    _is_usable_ipv4,
    _resolve_ip_overrides,
)


def _decode(props: dict[bytes, bytes]) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in props.items()}


class TestBuildProperties:
    def test_required_fields_always_present(self) -> None:
        props = _decode(HoloAnnouncer().build_properties())
        assert props["v"] == TXT_SCHEMA_VERSION
        assert props["holo_version"] == __version__
        assert "host" in props
        assert "user" in props
        assert "holo_pid" in props
        assert "started" in props
        assert "cwd" in props

    def test_session_omitted_when_not_specified(self) -> None:
        props = _decode(HoloAnnouncer().build_properties())
        assert "session" not in props

    def test_session_included_when_specified(self) -> None:
        props = _decode(HoloAnnouncer(session="my-session").build_properties())
        assert props["session"] == "my-session"

    def test_ssh_user_omitted_when_not_specified(self) -> None:
        props = _decode(HoloAnnouncer().build_properties())
        assert "ssh_user" not in props

    def test_ssh_user_included_when_specified(self) -> None:
        props = _decode(HoloAnnouncer(ssh_user="brad-remote").build_properties())
        assert props["ssh_user"] == "brad-remote"

    def test_user_explicit_overrides_default(self) -> None:
        props = _decode(HoloAnnouncer(user="alice").build_properties())
        assert props["user"] == "alice"

    def test_user_defaults_to_getpass(self) -> None:
        with patch("holo.announce.getpass.getuser", return_value="default-user"):
            props = _decode(HoloAnnouncer().build_properties())
        assert props["user"] == "default-user"


class TestTmuxDetection:
    def test_tmux_fields_omitted_when_not_in_tmux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TMUX", raising=False)
        props = _decode(HoloAnnouncer().build_properties())
        assert "tmux_session" not in props
        assert "tmux_window" not in props

    def test_tmux_fields_included_when_in_tmux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,3")

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            mapping = {"#S": "claude-1", "#W": "main"}
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=mapping[cmd[-1]] + "\n"
            )

        with patch("holo.announce.subprocess.run", side_effect=fake_run):
            props = _decode(HoloAnnouncer().build_properties())

        assert props["tmux_session"] == "claude-1"
        assert props["tmux_window"] == "main"

    def test_tmux_field_failure_falls_back_to_omit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,3")

        def failing_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="")

        with patch("holo.announce.subprocess.run", side_effect=failing_run):
            props = _decode(HoloAnnouncer().build_properties())

        assert "tmux_session" not in props
        assert "tmux_window" not in props


class TestIPCollection:
    def test_is_usable_ipv4_filters_loopback(self) -> None:
        assert not _is_usable_ipv4("127.0.0.1")
        assert not _is_usable_ipv4("127.5.6.7")

    def test_is_usable_ipv4_filters_link_local(self) -> None:
        assert not _is_usable_ipv4("169.254.0.1")
        assert not _is_usable_ipv4("169.254.99.99")

    def test_is_usable_ipv4_accepts_lan(self) -> None:
        assert _is_usable_ipv4("192.168.1.106")
        assert _is_usable_ipv4("10.0.0.5")
        assert _is_usable_ipv4("172.16.5.5")

    def test_is_usable_ipv4_rejects_garbage(self) -> None:
        assert not _is_usable_ipv4("not-an-ip")
        assert not _is_usable_ipv4("999.999.999.999")
        assert not _is_usable_ipv4("1.2.3")
        assert not _is_usable_ipv4("")

    def test_enumerate_skips_loopback_and_link_local(self) -> None:
        # Build a fake ifaddr.get_adapters() that returns one
        # adapter with a loopback, a link-local, and a real LAN IP.
        class FakeIP:
            def __init__(self, ip: str) -> None:
                self.ip = ip

        class FakeAdapter:
            def __init__(self, ips: list[str]) -> None:
                self.ips = [FakeIP(ip) for ip in ips]

        with patch(
            "ifaddr.get_adapters",
            return_value=[
                FakeAdapter(["127.0.0.1", "169.254.5.5", "192.168.1.7"])
            ],
        ):
            ips = _enumerate_local_ipv4()
        assert ips == ["192.168.1.7"]

    def test_enumerate_dedupes(self) -> None:
        class FakeIP:
            def __init__(self, ip: str) -> None:
                self.ip = ip

        class FakeAdapter:
            def __init__(self, ips: list[str]) -> None:
                self.ips = [FakeIP(ip) for ip in ips]

        with patch(
            "ifaddr.get_adapters",
            return_value=[
                FakeAdapter(["192.168.1.7"]),
                FakeAdapter(["192.168.1.7", "10.0.0.5"]),
            ],
        ):
            ips = _enumerate_local_ipv4()
        assert ips == ["192.168.1.7", "10.0.0.5"]

    def test_enumerate_filters_ipv6_tuples(self) -> None:
        class FakeIP:
            def __init__(self, ip: Any) -> None:
                self.ip = ip

        class FakeAdapter:
            def __init__(self, ips: list[Any]) -> None:
                self.ips = [FakeIP(ip) for ip in ips]

        with patch(
            "ifaddr.get_adapters",
            return_value=[
                FakeAdapter([("fe80::1", 0, 6), "192.168.1.7"])
            ],
        ):
            ips = _enumerate_local_ipv4()
        assert ips == ["192.168.1.7"]


class TestResolveIPOverrides:
    """`--announce-ip` accepts a mixed list of full IPs and trailing-dot
    prefixes. Prefixes filter the enumerated interface set; full IPs go
    through unchanged."""

    def test_full_ip_passes_through_without_enumeration(self) -> None:
        # No prefix in the list → enumeration must NOT be called.
        with patch(
            "holo.announce._enumerate_local_ipv4"
        ) as enum_mock:
            assert _resolve_ip_overrides(["10.0.0.5"]) == ["10.0.0.5"]
        assert not enum_mock.called

    def test_prefix_matches_enumerated(self) -> None:
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=["192.168.1.111", "192.168.1.15", "10.0.0.5"],
        ):
            assert _resolve_ip_overrides(["192.168.1."]) == [
                "192.168.1.111",
                "192.168.1.15",
            ]

    def test_short_prefix_matches_broadly(self) -> None:
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=["192.168.1.111", "192.168.5.5", "10.0.0.5"],
        ):
            assert _resolve_ip_overrides(["192."]) == [
                "192.168.1.111",
                "192.168.5.5",
            ]

    def test_prefix_with_no_match_returns_empty(self) -> None:
        # Intentional: the user said "advertise only this subnet"; if
        # that subnet isn't on any interface, advertise nothing rather
        # than silently widening the broadcast.
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=["192.168.1.111"],
        ):
            assert _resolve_ip_overrides(["10.0.0."]) == []

    def test_mixed_full_and_prefix_preserves_order(self) -> None:
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=["192.168.1.111", "10.0.0.5"],
        ):
            # Prefix entry comes first → matched IPs come first;
            # literal entry follows → appended verbatim.
            assert _resolve_ip_overrides(["192.168.1.", "8.8.8.8"]) == [
                "192.168.1.111",
                "8.8.8.8",
            ]

    def test_dedupe_across_prefix_and_literal(self) -> None:
        # If the same IP comes via both prefix-match and literal, it
        # should appear once at the position of the first occurrence.
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=["192.168.1.111"],
        ):
            assert _resolve_ip_overrides(
                ["192.168.1.", "192.168.1.111"]
            ) == ["192.168.1.111"]

    def test_empty_string_entries_skipped(self) -> None:
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=["10.0.0.5"],
        ):
            assert _resolve_ip_overrides(["", "10.0.0.5", ""]) == [
                "10.0.0.5"
            ]


class TestCollectIPsWithPrefixOverride:
    """`HoloAnnouncer._collect_ips` integrates the prefix resolver
    with the existing _is_usable_ipv4 filter."""

    def test_prefix_filters_to_lan_address(self) -> None:
        # The motivating case: a remote box with a VPN tunnel address
        # at index 0 sometimes broadcasts to a discoverer that can only
        # reach the LAN address. `--announce-ip 192.168.1.` lets the
        # operator pick.
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=[
                "192.168.193.226",
                "192.168.1.111",
                "192.168.1.15",
            ],
        ):
            a = HoloAnnouncer(ips=["192.168.1."])
            assert a._collect_ips() == ["192.168.1.111", "192.168.1.15"]

    def test_prefix_resolution_runs_through_is_usable_filter(self) -> None:
        # If a prefix happens to match a loopback address (constructed
        # carefully — _enumerate_local_ipv4 already strips loopback,
        # but _is_usable_ipv4 is the load-bearing safety net for the
        # literal-IP path and must not be skipped on this code path).
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=["10.0.0.5"],
        ):
            a = HoloAnnouncer(ips=["127.0.0.1"])
            # Literal loopback in override is still filtered out.
            assert a._collect_ips() == []


class TestIPsInTXTRecord:
    def test_ips_field_present_when_any_local_ipv4(self) -> None:
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=["192.168.1.7", "10.0.0.5"],
        ):
            props = _decode(HoloAnnouncer().build_properties())
        assert props["ips"] == "192.168.1.7,10.0.0.5"

    def test_ips_override_replaces_enumeration(self) -> None:
        # Enumeration returns one IP, but the override list takes
        # precedence and is what shows up in TXT.
        with patch(
            "holo.announce._enumerate_local_ipv4",
            return_value=["192.168.1.7"],
        ):
            props = _decode(
                HoloAnnouncer(ips=["10.0.0.5", "10.0.0.6"]).build_properties()
            )
        assert props["ips"] == "10.0.0.5,10.0.0.6"

    def test_ips_override_filters_invalid(self) -> None:
        # Loopback/link-local in the override list are silently dropped
        # (the user shouldn't be able to advertise unreachable IPs).
        props = _decode(
            HoloAnnouncer(
                ips=["127.0.0.1", "169.254.1.2", "10.0.0.5"]
            ).build_properties()
        )
        assert props["ips"] == "10.0.0.5"

    def test_ips_field_omitted_when_no_addresses(self) -> None:
        with (
            patch("holo.announce._enumerate_local_ipv4", return_value=[]),
            patch(
                "holo.announce.socket.gethostbyname",
                side_effect=OSError("no resolution"),
            ),
        ):
            props = _decode(HoloAnnouncer().build_properties())
        assert "ips" not in props


class TestInstanceName:
    """The instance label must fit in 63 bytes (DNS label cap, RFC 1035).

    Regression: GHA runner hostnames are ~60 bytes alone, which blows the
    limit when concatenated with the pid/salt suffix. zeroconf raises
    BadTypeInNameException at ServiceInfo construction even when the
    network layer is mocked.
    """

    def test_instance_name_fits_63_bytes_with_long_hostname(self) -> None:
        long_host = "sjc20-cw713-08a880d5-3c20-465c-85ab-7b7d1f6bcad4-064000B729D5"
        with patch("holo.announce.socket.gethostname", return_value=long_host):
            name = HoloAnnouncer()._instance_name()
        assert len(name) <= 63, f"instance name too long: {name!r} ({len(name)} bytes)"

    def test_instance_name_fits_63_bytes_with_long_session(self) -> None:
        long_session = "x" * 200
        name = HoloAnnouncer(session=long_session)._instance_name()
        assert len(name) <= 63, f"instance name too long: {name!r} ({len(name)} bytes)"

    def test_instance_name_preserves_session_prefix(self) -> None:
        name = HoloAnnouncer(session="claude-1")._instance_name()
        assert name.startswith("holo-claude-1-")


class TestCapabilitiesFields:
    """The optional caps_port / caps_token TXT fields."""

    def test_caps_fields_omitted_by_default(self) -> None:
        props = _decode(HoloAnnouncer().build_properties())
        assert "caps_port" not in props
        assert "caps_token" not in props

    def test_caps_fields_present_when_both_set(self) -> None:
        props = _decode(
            HoloAnnouncer(
                caps_port=49597, caps_token="abc-token"
            ).build_properties()
        )
        assert props["caps_port"] == "49597"
        assert props["caps_token"] == "abc-token"

    def test_caps_pair_validation_rejects_port_only(self) -> None:
        with pytest.raises(ValueError, match="must be set together"):
            HoloAnnouncer(caps_port=49597)

    def test_caps_pair_validation_rejects_token_only(self) -> None:
        with pytest.raises(ValueError, match="must be set together"):
            HoloAnnouncer(caps_token="abc")

    def test_caps_port_in_int_fields(self) -> None:
        # Sanity check: discover.py converts caps_port to int because
        # the field name is in INT_FIELDS. Verify the contract here so
        # adding the field can't accidentally drop the conversion.
        from holo.announce import FIELD_CAPS_PORT, INT_FIELDS

        assert FIELD_CAPS_PORT in INT_FIELDS


class TestServiceTypeConstants:
    def test_service_type_format(self) -> None:
        assert SERVICE_TYPE == "_holo-session._tcp.local."

    def test_schema_version_string(self) -> None:
        # TXT values are bytes in the wire protocol; we serialize them
        # as UTF-8 strings here, so the schema version must be a string
        # constant (not an int) to feed straight into the dict.
        assert isinstance(TXT_SCHEMA_VERSION, str)
        assert TXT_SCHEMA_VERSION == "1"


class TestLifecycle:
    def test_stop_before_start_is_safe(self) -> None:
        a = HoloAnnouncer()
        a.stop()  # should not raise

    def test_double_stop_is_safe(self) -> None:
        a = HoloAnnouncer()
        a.stop()
        a.stop()

    def test_start_registers_with_zeroconf(self) -> None:
        with patch("zeroconf.Zeroconf") as zc_cls:
            instance = zc_cls.return_value
            a = HoloAnnouncer(session="test")
            a.start()

        assert instance.register_service.called
        info = instance.register_service.call_args[0][0]
        assert info.type == SERVICE_TYPE
        assert info.name.endswith(SERVICE_TYPE)

    def test_stop_unregisters_and_closes(self) -> None:
        with patch("zeroconf.Zeroconf") as zc_cls:
            instance = zc_cls.return_value
            a = HoloAnnouncer()
            a.start()
            a.stop()

        assert instance.unregister_service.called
        assert instance.close.called

    def test_start_is_idempotent(self) -> None:
        with patch("zeroconf.Zeroconf") as zc_cls:
            instance = zc_cls.return_value
            a = HoloAnnouncer()
            a.start()
            a.start()  # second call should be a no-op

        assert instance.register_service.call_count == 1


class TestTunnelPort:
    """Phase 4 tunnel_port wiring on HoloAnnouncer."""

    def test_tunnel_port_omitted_by_default(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORT

        a = HoloAnnouncer()
        props = a.build_properties()
        assert FIELD_TUNNEL_PORT.encode() not in props

    def test_tunnel_port_appears_in_txt_when_set(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORT

        a = HoloAnnouncer()
        a.set_tunnel_port(54321)
        props = a.build_properties()
        assert props[FIELD_TUNNEL_PORT.encode()] == b"54321"

    def test_set_tunnel_port_before_start_persists_into_first_publish(
        self,
    ) -> None:
        from holo.announce import FIELD_TUNNEL_PORT

        a = HoloAnnouncer()
        a.set_tunnel_port(9001)  # before start — pure config update

        with patch("zeroconf.Zeroconf") as zc_cls:
            instance = zc_cls.return_value
            a.start()

        info = instance.register_service.call_args[0][0]
        assert info.properties[FIELD_TUNNEL_PORT.encode()] == b"9001"

    def test_set_tunnel_port_after_start_calls_update_service(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORT

        with patch("zeroconf.Zeroconf") as zc_cls, patch(
            "zeroconf.ServiceInfo"
        ) as si_cls:
            instance = zc_cls.return_value
            a = HoloAnnouncer()
            a.start()
            si_cls.reset_mock()
            instance.update_service.reset_mock()
            a.set_tunnel_port(12345)

        assert instance.update_service.called
        new_info = instance.update_service.call_args[0][0]
        # The new ServiceInfo got the tunnel_port field via build_properties.
        properties_call = si_cls.call_args.kwargs["properties"]
        assert properties_call[FIELD_TUNNEL_PORT.encode()] == b"12345"
        assert new_info is si_cls.return_value

    def test_set_tunnel_port_clear_after_publish(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORT

        with patch("zeroconf.Zeroconf") as zc_cls, patch(
            "zeroconf.ServiceInfo"
        ) as si_cls:
            instance = zc_cls.return_value
            a = HoloAnnouncer()
            a.start()
            a.set_tunnel_port(12345)
            si_cls.reset_mock()
            instance.update_service.reset_mock()
            a.set_tunnel_port(None)  # clear

        properties_call = si_cls.call_args.kwargs["properties"]
        assert FIELD_TUNNEL_PORT.encode() not in properties_call
        assert instance.update_service.called

    def test_set_tunnel_port_idempotent(self) -> None:
        with patch("zeroconf.Zeroconf") as zc_cls:
            instance = zc_cls.return_value
            a = HoloAnnouncer()
            a.start()
            a.set_tunnel_port(9001)
            instance.update_service.reset_mock()
            a.set_tunnel_port(9001)  # same value — no-op
        assert not instance.update_service.called

    def test_int_fields_includes_tunnel_port(self) -> None:
        """parse_txt-style consumers must coerce tunnel_port to int."""
        from holo.announce import FIELD_TUNNEL_PORT, INT_FIELDS

        assert FIELD_TUNNEL_PORT in INT_FIELDS


class TestTunnelPortsMap:
    """Phase 5b: multi-CloudCity tunnel_ports map."""

    def test_tunnel_ports_omitted_by_default(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORTS

        a = HoloAnnouncer()
        props = a.build_properties()
        assert FIELD_TUNNEL_PORTS.encode() not in props

    def test_tunnel_ports_in_txt_when_set(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORTS

        a = HoloAnnouncer()
        a.set_tunnel_ports({"cc-upstairs": 51492, "cc-office": 51493})
        props = a.build_properties()
        # Stable key order (sorted) — important for diff stability
        # across rebuilds.
        assert (
            props[FIELD_TUNNEL_PORTS.encode()]
            == b"cc-office:51493,cc-upstairs:51492"
        )

    def test_tunnel_ports_empty_dict_clears_field(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORTS

        a = HoloAnnouncer()
        a.set_tunnel_ports({"cc-upstairs": 51492})
        a.set_tunnel_ports({})
        props = a.build_properties()
        assert FIELD_TUNNEL_PORTS.encode() not in props

    def test_tunnel_ports_none_clears_field(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORTS

        a = HoloAnnouncer()
        a.set_tunnel_ports({"cc-upstairs": 51492})
        a.set_tunnel_ports(None)
        props = a.build_properties()
        assert FIELD_TUNNEL_PORTS.encode() not in props

    def test_tunnel_ports_idempotent(self) -> None:
        with patch("zeroconf.Zeroconf") as zc_cls:
            instance = zc_cls.return_value
            a = HoloAnnouncer()
            a.start()
            a.set_tunnel_ports({"cc-x": 51492})
            instance.update_service.reset_mock()
            a.set_tunnel_ports({"cc-x": 51492})  # same value — no-op
        assert not instance.update_service.called

    def test_tunnel_ports_after_start_calls_update_service(self) -> None:
        from holo.announce import FIELD_TUNNEL_PORTS

        with patch("zeroconf.Zeroconf") as zc_cls, patch(
            "zeroconf.ServiceInfo"
        ) as si_cls:
            instance = zc_cls.return_value
            a = HoloAnnouncer()
            a.start()
            si_cls.reset_mock()
            instance.update_service.reset_mock()
            a.set_tunnel_ports({"cc-x": 12345})

        assert instance.update_service.called
        properties_call = si_cls.call_args.kwargs["properties"]
        assert properties_call[FIELD_TUNNEL_PORTS.encode()] == b"cc-x:12345"

    def test_singular_and_map_can_coexist(self) -> None:
        """Mid-rollout: both fields present so old + new SPAs both route."""
        from holo.announce import FIELD_TUNNEL_PORT, FIELD_TUNNEL_PORTS

        a = HoloAnnouncer()
        a.set_tunnel_port(51492)
        a.set_tunnel_ports({"cc-x": 51492})
        props = a.build_properties()
        assert props[FIELD_TUNNEL_PORT.encode()] == b"51492"
        assert props[FIELD_TUNNEL_PORTS.encode()] == b"cc-x:51492"


class TestParseTunnelPorts:
    """The decode side that discoverers use."""

    def test_basic_parse(self) -> None:
        from holo.announce import parse_tunnel_ports

        assert parse_tunnel_ports("cc-A:51492,cc-B:51493") == {
            "cc-A": 51492,
            "cc-B": 51493,
        }

    def test_empty_returns_empty_dict(self) -> None:
        from holo.announce import parse_tunnel_ports

        assert parse_tunnel_ports("") == {}

    def test_strips_whitespace(self) -> None:
        from holo.announce import parse_tunnel_ports

        assert parse_tunnel_ports("  cc-A : 51492 , cc-B :51493 ") == {
            "cc-A": 51492,
            "cc-B": 51493,
        }

    def test_drops_malformed_entries(self) -> None:
        """Bad entries (no colon, non-int port, empty instance) drop;
        valid entries survive."""
        from holo.announce import parse_tunnel_ports

        result = parse_tunnel_ports(
            "cc-A:51492,no-colon,cc-B:not-an-int,:99,cc-C:51494"
        )
        assert result == {"cc-A": 51492, "cc-C": 51494}

    def test_preserves_instance_with_dashes_and_dots(self) -> None:
        from holo.announce import parse_tunnel_ports

        assert parse_tunnel_ports(
            "cloudcity-MacBook-Air-3-abc.local:51492"
        ) == {"cloudcity-MacBook-Air-3-abc.local": 51492}


class TestRemoteCommand:
    """Phase 5b+: `remote_command` TXT field with $TMUX / $STY auto-detect."""

    def test_omitted_when_no_override_and_no_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from holo.announce import FIELD_REMOTE_COMMAND

        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("STY", raising=False)
        a = HoloAnnouncer()
        props = a.build_properties()
        assert FIELD_REMOTE_COMMAND.encode() not in props

    def test_explicit_override_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from holo.announce import FIELD_REMOTE_COMMAND

        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        a = HoloAnnouncer(remote_command="screen -x my-shared")
        props = a.build_properties()
        assert (
            props[FIELD_REMOTE_COMMAND.encode()] == b"screen -x my-shared"
        )

    def test_empty_override_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from holo.announce import FIELD_REMOTE_COMMAND

        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("STY", raising=False)
        a = HoloAnnouncer(remote_command="")
        assert a.remote_command is None
        props = a.build_properties()
        assert FIELD_REMOTE_COMMAND.encode() not in props

    def test_tmux_env_auto_detect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When $TMUX is set, default `remote_command` is
        `tmux -u attach -t '<tmux #S>'`. ``-u`` forces UTF-8 mode for
        the attaching client (browser xterms can't always advertise
        UTF-8 via locale detection, and without it tmux downgrades
        chevrons / Unicode block art to ``__`` ASCII fallbacks)."""
        from holo.announce import FIELD_REMOTE_COMMAND

        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        monkeypatch.delenv("STY", raising=False)
        with patch("holo.announce._tmux_field", return_value="my-sess"):
            a = HoloAnnouncer()
            props = a.build_properties()
        assert (
            props[FIELD_REMOTE_COMMAND.encode()]
            == b"tmux -u attach -t 'my-sess'"
        )

    def test_screen_env_auto_detect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When $STY is set (and not $TMUX), default is `screen -r <name>`."""
        from holo.announce import FIELD_REMOTE_COMMAND

        monkeypatch.delenv("TMUX", raising=False)
        # GNU screen sets STY to "<pid>.<ttyname>.<hostname>".
        monkeypatch.setenv("STY", "12345.pts-0.host")
        a = HoloAnnouncer()
        props = a.build_properties()
        assert (
            props[FIELD_REMOTE_COMMAND.encode()]
            == b"screen -r pts-0.host"
        )

    def test_screen_env_without_dot_uses_whole_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive: unusual screen setups may leave STY without a dot.
        Fall through with the literal value rather than emitting bogus."""
        from holo.announce import FIELD_REMOTE_COMMAND

        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setenv("STY", "raw-session-name")
        a = HoloAnnouncer()
        props = a.build_properties()
        assert (
            props[FIELD_REMOTE_COMMAND.encode()]
            == b"screen -r raw-session-name"
        )

    def test_tmux_wins_when_both_envs_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Edge: $TMUX *and* $STY simultaneously (someone nested screen
        inside tmux or vice versa). tmux wins; matches the existing
        `tmux_session` field's environment priority."""
        from holo.announce import FIELD_REMOTE_COMMAND

        monkeypatch.setenv("TMUX", "/tmp/tmux/default,1,0")
        monkeypatch.setenv("STY", "12345.pts-0.host")
        with patch("holo.announce._tmux_field", return_value="from-tmux"):
            a = HoloAnnouncer()
            props = a.build_properties()
        assert (
            props[FIELD_REMOTE_COMMAND.encode()]
            == b"tmux -u attach -t 'from-tmux'"
        )

    def test_tmux_session_with_special_chars_single_quoted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default tmux command single-quotes the session name so spaces
        / wildcards survive the trip through the SPA + remote shell."""
        from holo.announce import FIELD_REMOTE_COMMAND

        monkeypatch.setenv("TMUX", "/tmp/tmux/default,1,0")
        with patch(
            "holo.announce._tmux_field", return_value="claude session"
        ):
            a = HoloAnnouncer()
            props = a.build_properties()
        assert (
            props[FIELD_REMOTE_COMMAND.encode()]
            == b"tmux -u attach -t 'claude session'"
        )


class TestCLIFlagParsing:
    """Validate the announce flag plumbing in `holo mcp`."""

    def test_announce_user_without_announce_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["mcp", "--announce-user", "alice"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "require --announce" in err

    def test_announce_session_without_announce_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["mcp", "--announce-session", "s1"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "require --announce" in err

    def test_announce_ssh_user_without_announce_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["mcp", "--announce-ssh-user", "remote"])
        assert rc == 2

    def test_announce_session_without_value_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["mcp", "--announce", "--announce-session"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--announce-session requires a value" in err

    def test_announce_session_followed_by_flag_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `--announce-session --announce-user x` — session is missing its value.
        from holo.cli import main

        rc = main(
            ["mcp", "--announce", "--announce-session", "--announce-user", "x"]
        )
        assert rc == 2

    def test_announce_ip_without_announce_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["mcp", "--announce-ip", "10.0.0.5"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "require --announce" in err

    def test_announce_ip_parses_comma_separated_list(self) -> None:
        from holo.cli import main

        with patch("holo.mcp_server.run") as run:
            main(
                [
                    "mcp",
                    "--announce",
                    "--announce-ip",
                    "10.0.0.5,192.168.1.7",
                    "--no-bookmarklet",
                ]
            )
            kwargs = run.call_args.kwargs
            assert kwargs["announce_ips"] == ["10.0.0.5", "192.168.1.7"]

    def test_announce_ip_strips_whitespace(self) -> None:
        from holo.cli import main

        with patch("holo.mcp_server.run") as run:
            main(
                [
                    "mcp",
                    "--announce",
                    "--announce-ip",
                    "10.0.0.5, 192.168.1.7 ,",
                    "--no-bookmarklet",
                ]
            )
            kwargs = run.call_args.kwargs
            assert kwargs["announce_ips"] == ["10.0.0.5", "192.168.1.7"]

    def test_announce_ip_empty_string_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(
            ["mcp", "--announce", "--announce-ip", ",,", "--no-bookmarklet"]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "at least one IP" in err

    def test_announce_kwargs_threaded_to_run(self) -> None:
        from holo.cli import main

        with patch("holo.mcp_server.run") as run:
            main(
                [
                    "mcp",
                    "--announce",
                    "--announce-session",
                    "sess-x",
                    "--announce-user",
                    "user-x",
                    "--announce-ssh-user",
                    "ssh-x",
                    "--no-bookmarklet",
                ]
            )
            assert run.called
            kwargs = run.call_args.kwargs
            assert kwargs["announce"] is True
            assert kwargs["announce_session"] == "sess-x"
            assert kwargs["announce_user"] == "user-x"
            assert kwargs["announce_ssh_user"] == "ssh-x"

    def test_announce_kwargs_threaded_to_run_tcp(self) -> None:
        from holo.cli import main

        with patch("holo.mcp_server.run_tcp") as run_tcp:
            main(
                [
                    "mcp",
                    "--listen",
                    "7777",
                    "--announce",
                    "--announce-session",
                    "sess-y",
                    "--no-bookmarklet",
                ]
            )
            assert run_tcp.called
            kwargs = run_tcp.call_args.kwargs
            assert kwargs["announce"] is True
            assert kwargs["announce_session"] == "sess-y"
            # Ungiven optional flags pass through as None.
            assert kwargs["announce_user"] is None
            assert kwargs["announce_ssh_user"] is None


class TestCapabilitiesCLIFlags:
    """Validate --announce-capabilities CLI plumbing.

    The per-probe flags `--probe-software` and `--probe-pkg` were removed
    in 0.1.0a16; the CLI now errors helpfully if anyone passes them so
    pasted-from-old-docs commands don't fail with a confusing
    "unrecognised flag".
    """

    def test_announce_capabilities_without_announce_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["mcp", "--announce-capabilities"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "require --announce" in err

    def test_legacy_probe_software_flag_errors_with_migration_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(
            [
                "mcp",
                "--announce",
                "--announce-capabilities",
                "--probe-software",
                "ffmpeg,git",
                "--no-bookmarklet",
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "removed in 0.1.0a16" in err
        assert "auto-discovered" in err

    def test_legacy_probe_pkg_flag_errors_with_migration_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(
            [
                "mcp",
                "--announce",
                "--announce-capabilities",
                "--probe-pkg",
                "brew",
                "--no-bookmarklet",
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "removed in 0.1.0a16" in err

    def test_capabilities_kwargs_threaded_to_run(self) -> None:
        from holo.cli import main

        with patch("holo.mcp_server.run") as run:
            main(
                [
                    "mcp",
                    "--announce",
                    "--announce-capabilities",
                    "--no-bookmarklet",
                ]
            )
            kwargs = run.call_args.kwargs
            assert kwargs["announce_capabilities"] is True
            # The per-probe kwargs are gone — verify they're not
            # being threaded through.
            assert "probe_software" not in kwargs
            assert "probe_packages" not in kwargs

    def test_capabilities_kwargs_threaded_to_run_tcp(self) -> None:
        from holo.cli import main

        with patch("holo.mcp_server.run_tcp") as run_tcp:
            main(
                [
                    "mcp",
                    "--listen",
                    "7778",
                    "--announce",
                    "--announce-capabilities",
                    "--no-bookmarklet",
                ]
            )
            kwargs = run_tcp.call_args.kwargs
            assert kwargs["announce_capabilities"] is True
            assert "probe_software" not in kwargs
            assert "probe_packages" not in kwargs


class TestHoloMCPServerIntegration:
    def test_no_announce_means_no_announcer(self) -> None:
        from holo.mcp_server import HoloMCPServer

        server = HoloMCPServer(no_bookmarklet=True)
        assert server._announcer is None
        server.shutdown()

    def test_announce_constructs_announcer(self) -> None:
        from holo.mcp_server import HoloMCPServer

        with patch("holo.announce.HoloAnnouncer") as ann_cls:
            server = HoloMCPServer(
                no_bookmarklet=True,
                announce=True,
                announce_session="sess-x",
                announce_user="user-x",
                announce_ssh_user="ssh-x",
            )
            assert ann_cls.called
            kwargs = ann_cls.call_args.kwargs
            assert kwargs["session"] == "sess-x"
            assert kwargs["user"] == "user-x"
            assert kwargs["ssh_user"] == "ssh-x"
            assert ann_cls.return_value.start.called
            server.shutdown()
            assert ann_cls.return_value.stop.called

    def test_announcer_failure_does_not_break_server(self) -> None:
        from holo.mcp_server import HoloMCPServer

        with patch("holo.announce.HoloAnnouncer") as ann_cls:
            ann_cls.return_value.start.side_effect = RuntimeError("net is down")
            # Should swallow the error and continue with announcer=None.
            server = HoloMCPServer(no_bookmarklet=True, announce=True)
            assert server._announcer is None
            server.shutdown()

    def test_capabilities_flag_constructs_caps_server(self) -> None:
        """When --announce-capabilities is on, the HoloMCPServer must
        stand up the caps server *before* the announcer and pass the
        bound port + token through."""
        from holo.mcp_server import HoloMCPServer

        # Stub both classes so we don't actually open sockets / multicast.
        with (
            patch(
                "holo.capabilities_server.CapabilitiesServer"
            ) as caps_cls,
            patch("holo.announce.HoloAnnouncer") as ann_cls,
        ):
            caps_instance = caps_cls.return_value
            caps_instance.actual_port = 49597
            caps_instance.token = "fake-token-xyz"

            server = HoloMCPServer(
                no_bookmarklet=True,
                announce=True,
                announce_capabilities=True,
            )

            # Caps server constructed and started.
            assert caps_cls.called
            assert caps_instance.start.called
            # Announcer received the caps_port + caps_token.
            ann_kwargs = ann_cls.call_args.kwargs
            assert ann_kwargs["caps_port"] == 49597
            assert ann_kwargs["caps_token"] == "fake-token-xyz"

            server.shutdown()
            # Both stopped on shutdown.
            assert caps_instance.stop.called
            assert ann_cls.return_value.stop.called

    def test_capabilities_disabled_means_no_caps_server(self) -> None:
        """When --announce-capabilities is off, no caps server is built
        and the announcer gets caps_port=None / caps_token=None."""
        from holo.mcp_server import HoloMCPServer

        with (
            patch(
                "holo.capabilities_server.CapabilitiesServer"
            ) as caps_cls,
            patch("holo.announce.HoloAnnouncer") as ann_cls,
        ):
            server = HoloMCPServer(
                no_bookmarklet=True,
                announce=True,
                announce_capabilities=False,
            )

            assert not caps_cls.called
            ann_kwargs = ann_cls.call_args.kwargs
            assert ann_kwargs["caps_port"] is None
            assert ann_kwargs["caps_token"] is None
            server.shutdown()

    def test_capabilities_skipped_without_announce(self) -> None:
        """Even if the kwarg slips through (CLI rejects it), the server
        should refuse to stand up the caps endpoint without announce —
        the URL would be unreachable without the TXT broadcast."""
        from holo.mcp_server import HoloMCPServer

        with patch("holo.capabilities_server.CapabilitiesServer") as caps_cls:
            server = HoloMCPServer(
                no_bookmarklet=True,
                announce=False,
                announce_capabilities=True,
            )
            assert not caps_cls.called
            server.shutdown()


class TestSIGTERMHandling:
    """SIGTERM must trigger graceful shutdown so the announcer sends
    mDNS Goodbye packets before the process exits."""

    def test_handler_installed_and_restored(self) -> None:
        import signal

        from holo.mcp_server import _sigterm_as_keyboard_interrupt

        previous = signal.getsignal(signal.SIGTERM)
        with _sigterm_as_keyboard_interrupt():
            inside = signal.getsignal(signal.SIGTERM)
            assert inside is not previous
        after = signal.getsignal(signal.SIGTERM)
        assert after is previous

    def test_handler_raises_keyboard_interrupt(self) -> None:
        import signal

        from holo.mcp_server import _sigterm_as_keyboard_interrupt

        with _sigterm_as_keyboard_interrupt():
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)

    def test_run_invokes_shutdown_on_keyboard_interrupt(self) -> None:
        """`mcp.run` raising KeyboardInterrupt must still trigger
        `holo.shutdown()` so the announcer's Goodbye packet goes out.
        """
        from holo import mcp_server

        def _raise_kbi(self: Any) -> None:
            raise KeyboardInterrupt()

        fake_mcp = type("FakeMCP", (), {"run": _raise_kbi})()
        fake_holo = type("FakeHolo", (), {})()
        fake_holo.shutdown_called = False

        def _shutdown() -> None:
            fake_holo.shutdown_called = True

        fake_holo.shutdown = _shutdown

        with patch.object(
            mcp_server, "build_server", return_value=(fake_mcp, fake_holo)
        ):
            mcp_server.run(no_bookmarklet=True)

        assert fake_holo.shutdown_called

    def test_handler_skipped_off_main_thread(self) -> None:
        """`signal.signal` raises ValueError off the main thread; the
        context manager must swallow that and yield (graceful shutdown
        falls back to SIGINT only)."""
        import signal as signal_mod

        from holo.mcp_server import _sigterm_as_keyboard_interrupt

        with patch.object(
            signal_mod, "signal", side_effect=ValueError("not main thread")
        ):
            # Should not raise.
            with _sigterm_as_keyboard_interrupt():
                pass
