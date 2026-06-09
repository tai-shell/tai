"""Reverse-tunnel orchestration (Phase 4 of the CloudCity tunnel spec).

Owns the ssh subprocess that exposes this holo daemon's local sshd
(port 22) inside a remote CloudCity host's loopback. The desktop SPA's
c2w VM then connects to ``localhost:<tunnel_port>`` from inside
c2w-net — the forward lands on this daemon's :22, and the user's
tmux session is reachable.

Topology recap (from the spec):

    [Host B: this daemon]                 [Host A: CloudCity]
       sshd :22  ◄────── -R 0:localhost:22 ────── sshd :2222
       holo daemon ────── ssh -A -N ─────────►   c2w-net loopback :<allocated>

The ssh subprocess uses ``-R 0:localhost:22`` so sshd on the
CloudCity end allocates a free port; we parse the
``Allocated port N for remote forward to localhost:22`` line that
sshd writes to the client's stderr to discover ``N``.

Spec:
  https://github.com/bradclarkalexander/desktop/blob/develop/docs/holo-cloudcity-tunnel-spec.md §4.4
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from holo import cert as cert_mod
from holo import cloudcity_announce as cc_announce

_log = logging.getLogger(__name__)


# Default principal used when SSHing into the CloudCity host. C2w-net's
# stock /etc/ssh/sshd_config + /etc/ssh/ca.pub accepts any cert with
# this principal regardless of which trusted CA signed it. Override
# only if a custom c2w-net image uses a different lando-equivalent.
DEFAULT_PRINCIPAL = "lando"

# Time we wait for sshd's "Allocated port N" line before giving up.
# 15s covers a slow first-connect TLS handshake + ssh banner + auth
# but is short enough that a wedged tunnel surfaces fast.
_PORT_ANNOUNCE_TIMEOUT_S = 15.0

# How long ``stop()`` waits for the ssh subprocess to exit gracefully
# before issuing SIGKILL. Most well-behaved sshd backends close their
# end on SIGTERM within a second; 5s is generous.
_STOP_TIMEOUT_S = 5.0

# Match a line from ssh's stderr like
#   "Allocated port 51492 for remote forward to localhost:22"
_ALLOCATED_PORT_RE = re.compile(
    r"Allocated port (\d+) for remote forward"
)


class TunnelError(Exception):
    """Base error for tunnel lifecycle problems."""


class TunnelStartTimeout(TunnelError):
    """Raised when ssh doesn't print the allocated port within the timeout."""


class TunnelExited(TunnelError):
    """Raised when ssh exits before reporting a port (auth fail, network, etc.)."""


def _pick_ip(record: dict[str, Any]) -> str:
    """Return the first IP from a CloudCity record, or raise.

    v1 picks the announced order verbatim — the announcer is
    responsible for listing ZT/preferred addresses first. A future
    ranking pass can reorder by reachability probe.
    """
    ips = record.get(cc_announce.FIELD_IPS) or []
    if not ips:
        host = record.get(cc_announce.FIELD_HOST)
        if host:
            return host
        raise TunnelError(
            f"CloudCity record {record.get('instance')!r} has no ips or host"
        )
    return ips[0]


def _build_ssh_argv(
    *,
    target_ip: str,
    target_port: int,
    key_path: Path,
    principal: str,
    local_port: int,
    extra_ssh_args: list[str] | None = None,
) -> list[str]:
    """Build the argv for the ssh subprocess.

    Notable flags:
      -N             do not run a remote command (forward-only session)
      -A             forward the local agent — the cert is presented
                     via the agent forwarding chain to whatever the
                     remote shell wants to do next
      -i KEY         use the holo daemon's keypair; OpenSSH auto-picks
                     up the matching <key>-cert.pub
      -o IdentitiesOnly=yes      don't try every key in the agent first
      -o BatchMode=yes           never prompt; fail-fast on missing creds
      -o ExitOnForwardFailure=yes  bail if the -R can't be set up
      -o ServerAliveInterval=30  keep the tunnel alive across NAT timeouts
      -o StrictHostKeyChecking=accept-new  TOFU on first connect; reject
                                            mid-session host-key changes
      -R local_port:localhost:22  the actual reverse forward
    """
    argv = [
        "ssh",
        "-N",
        "-A",
        "-i", str(key_path),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "StrictHostKeyChecking=accept-new",
        "-R", f"{local_port}:localhost:22",
        "-p", str(target_port),
    ]
    if extra_ssh_args:
        argv.extend(extra_ssh_args)
    argv.append(f"{principal}@{target_ip}")
    return argv


class Tunnel:
    """Lifecycle wrapper around one ``ssh -R`` subprocess.

    Use ``start()`` to bring it up (blocks until sshd reports the
    allocated port, then returns it). Use ``stop()`` to terminate.
    Both calls are idempotent.

    The ssh subprocess is spawned with ``stderr=PIPE`` so we can
    parse the port announcement line. After ``start()`` resolves,
    the stderr pump thread keeps draining so the pipe doesn't fill
    up and block ssh — drained lines are forwarded to the holo
    logger at INFO so operators can see what's happening.
    """

    def __init__(
        self,
        *,
        cloudcity_record: dict[str, Any],
        key_path: Path = cert_mod.DEFAULT_KEY_PATH,
        principal: str = DEFAULT_PRINCIPAL,
        local_port: int = 0,
        extra_ssh_args: list[str] | None = None,
    ) -> None:
        self.record = cloudcity_record
        self.key_path = key_path
        self.principal = principal
        self.local_port = local_port  # 0 = let sshd allocate
        self.extra_ssh_args = list(extra_ssh_args) if extra_ssh_args else None

        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
        self._target_ip: str | None = None
        self._target_port: int | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_event = threading.Event()
        self._stderr_lock = threading.Lock()

    @property
    def port(self) -> int | None:
        """Allocated forward port on the CloudCity end, or ``None`` if not started."""
        return self._port

    @property
    def target(self) -> tuple[str, int] | None:
        """``(ip, port)`` of the CloudCity sshd we connected to."""
        if self._target_ip is None or self._target_port is None:
            return None
        return self._target_ip, self._target_port

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, *, timeout: float = _PORT_ANNOUNCE_TIMEOUT_S) -> int:
        """Spawn the ssh subprocess and block until the port is announced.

        Returns the allocated tunnel port on success. Raises
        ``TunnelStartTimeout`` or ``TunnelExited`` on failure — the
        process is reaped before either is raised.
        """
        if self._proc is not None:
            assert self._port is not None
            return self._port

        target_ip = _pick_ip(self.record)
        target_port = int(self.record.get(cc_announce.FIELD_PORT, 22))
        argv = _build_ssh_argv(
            target_ip=target_ip,
            target_port=target_port,
            key_path=self.key_path,
            principal=self.principal,
            local_port=self.local_port,
            extra_ssh_args=self.extra_ssh_args,
        )
        _log.info("tunnel: spawning ssh argv=%s", argv)

        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            # ssh uses /dev/tty for password prompts — close that path so
            # BatchMode=yes is the only failure mode for missing creds.
            start_new_session=True,
        )
        self._proc = proc
        self._target_ip = target_ip
        self._target_port = target_port

        # Drain stderr in a background thread so it never blocks ssh,
        # while looking for the "Allocated port N" announcement. Once
        # the port is found, the thread keeps draining (forwarding
        # lines to the logger) until the process dies.
        self._stderr_event.clear()
        self._stderr_thread = threading.Thread(
            target=self._pump_stderr,
            args=(proc,),
            name="holo-tunnel-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

        # Wait until we either see the port, the process exits, or
        # we time out. The pump thread sets `_stderr_event` whenever
        # the port is announced or stderr closes (process exiting).
        deadline = time.monotonic() + timeout
        while True:
            if self._port is not None:
                return self._port
            if proc.poll() is not None:
                # ssh exited before announcing a port — typically
                # auth fail, ExitOnForwardFailure tripping, or
                # connection refused.
                code = proc.returncode
                self._proc = None
                raise TunnelExited(
                    f"ssh exited with code {code} before announcing a "
                    f"forward port (target {target_ip}:{target_port})"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_proc(proc)
                self._proc = None
                raise TunnelStartTimeout(
                    f"ssh did not announce a forward port within "
                    f"{timeout:.1f}s (target {target_ip}:{target_port})"
                )
            self._stderr_event.wait(timeout=min(0.5, remaining))
            self._stderr_event.clear()

    def stop(self) -> None:
        """Tear down the tunnel. Idempotent."""
        proc = self._proc
        thread = self._stderr_thread
        self._proc = None
        self._port = None
        self._stderr_thread = None
        if proc is not None and proc.poll() is None:
            self._terminate_proc(proc)
        if thread is not None:
            thread.join(timeout=2.0)

    def _terminate_proc(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _log.warning("tunnel: ssh ignored SIGTERM; sending SIGKILL")
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _log.error("tunnel: ssh refused to die after SIGKILL")

    def _pump_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        """Read sshd's stderr line by line until it closes.

        Side-effects:
          - sets ``self._port`` once the "Allocated port N" line is seen
          - sets ``self._stderr_event`` after each line so ``start()``
            wakes up promptly when the port arrives
          - forwards every line to the module logger at INFO so
            operators can see what ssh is doing
        """
        assert proc.stderr is not None
        try:
            for raw_line in iter(proc.stderr.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue
                _log.info("tunnel-ssh: %s", line)
                if self._port is None:
                    m = _ALLOCATED_PORT_RE.search(line)
                    if m:
                        with self._stderr_lock:
                            self._port = int(m.group(1))
                self._stderr_event.set()
        finally:
            self._stderr_event.set()


def open_to_cloudcity(
    cloudcity_record: dict[str, Any],
    *,
    backend: str | None = None,
    key_path: Path = cert_mod.DEFAULT_KEY_PATH,
    principal: str = DEFAULT_PRINCIPAL,
    extra_ssh_args: list[str] | None = None,
    cert_force_refresh: bool = False,
) -> Tunnel:
    """Convenience: ensure a fresh cert, start the tunnel, return it.

    Caller owns the returned ``Tunnel`` and must ``stop()`` it. This
    helper is what ``holo tunnel up`` and the future ``holo_tunnel_up``
    MCP tool both call.
    """
    cert_mod.get_or_refresh(
        backend=backend, key_path=key_path, force=cert_force_refresh
    )
    tunnel = Tunnel(
        cloudcity_record=cloudcity_record,
        key_path=key_path,
        principal=principal,
        extra_ssh_args=extra_ssh_args,
    )
    tunnel.start()
    return tunnel


def find_cloudcity(
    instance: str,
    *,
    wait_s: float = 1.5,
) -> dict[str, Any] | None:
    """Browse the LAN for ``_cloudcity._tcp.local.`` and return one record.

    Matches by exact ``instance`` label first, then by ``host`` field
    as a fallback (so users can pass either form). Returns ``None`` if
    nothing matching shows up before ``wait_s`` elapses.
    """
    from holo import cloudcity_discover

    zc, _browser, store = cloudcity_discover._start_browser()
    try:
        # Wait up to wait_s, polling for our target so we don't always
        # eat the full timeout when the announcer is already cached.
        deadline = time.monotonic() + wait_s
        match: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            for r in store.snapshot():
                if r.get("instance") == instance:
                    match = r
                    break
                if r.get(cc_announce.FIELD_HOST) == instance:
                    match = r
                    break
            if match is not None:
                break
            time.sleep(0.1)
        return match
    finally:
        zc.close()


def parse_principal_from_env() -> str:
    """Allow ops override via env var, fall back to spec default."""
    return os.environ.get("HOLO_TUNNEL_PRINCIPAL") or DEFAULT_PRINCIPAL


__all__ = [
    "DEFAULT_PRINCIPAL",
    "Tunnel",
    "TunnelError",
    "TunnelExited",
    "TunnelStartTimeout",
    "find_cloudcity",
    "open_to_cloudcity",
    "parse_principal_from_env",
]
