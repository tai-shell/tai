# holo companion app — protocol spec

This document is the wire-level contract between the `holo` daemon and a
desktop companion app. It is intentionally implementation-agnostic — any
language, any UI framework, any platform — and is meant to be copied into
the companion's repo and used as the build reference.

The companion app discovers live `holo mcp` sessions on the local network
and provides a UI for connecting to them (SSH + tmux). The companion is
**not** part of the holo repo and is not part of holo's release process.

> **Source of truth for the broadcaster:** `src/holo/announce.py` in the
> holo repo (`https://github.com/nospaceleftondevice/holo`). This spec
> describes schema version `v=1`, current as of holo `0.1.0a18`. If the
> code and this doc disagree, the code wins.
>
> **Reference consumer:** `holo discover` (`src/holo/discover.py`) is the
> in-tree implementation of this contract. It exposes the same data the
> desktop companion needs through three modes — `--json` (snapshot),
> `--tail` (JSONL events), and `--serve PORT` (HTTP + WebSocket).
> Cross-check ambiguous wording in this spec against `discover.py` and
> the test suite (`tests/test_discover.py`).

---

## 1. Purpose

A user runs `holo mcp --announce` inside a tmux session on some machine
(usually a remote dev box reached over SSH). The CLI agent (Claude Code,
Codex, etc.) drives `holo`. The companion app — running on the user's
laptop — needs to:

1. **Discover** that this `holo` instance exists and where it lives.
2. **Display** human-readable metadata (host, user, session, project
   directory) so the user can pick a session.
3. **Connect** by spawning an SSH process to the right host, attaching to
   the right tmux session.

No authentication material is broadcast; SSH keys / agent forwarding /
known_hosts handle that out of band.

---

## 2. mDNS contract

### Service type

```
_holo-session._tcp.local.
```

DNS-SD form. The companion subscribes to this type and receives
add/remove events for every running `holo mcp --announce` instance on
the link-local network.

### Discovery scope

mDNS is **link-local** — it does not cross routers. Companion and
daemon must be on the same broadcast domain (typical home/office LAN,
same Wi-Fi SSID, same VPN segment). Out of scope: WAN discovery.

### Instance name

Each running daemon registers under a unique instance label of the
form:

```
holo-<session-or-host>-<pid>-<salt>
```

- `<session-or-host>` is `--announce-session NAME` if the user passed
  it, otherwise the daemon's short hostname.
- `<pid>` is the daemon's OS process id.
- `<salt>` is a 6-char hex UUID slice — collision avoidance without a
  rename round-trip.

The full instance label is capped at **63 bytes** (DNS label limit,
RFC 1035). The daemon truncates the human-readable head to fit.

### SRV record (port semantics)

| Mode | Port |
|---|---|
| `holo mcp --announce` (stdio) | `0` |
| `holo mcp --listen N --announce` (TCP) | `N` |

The companion **does not connect to this port**. Holo's TCP listener
is for `holo connect HOST:PORT` proxies, not for the companion.
The port is metadata for diagnostic display only.

### A records (IPv4)

The daemon enumerates every non-loopback, non-link-local IPv4 address
across all interfaces (via `ifaddr`) and advertises them as A records.
A `--announce-ip A,B,C` flag overrides auto-enumeration.

The companion *can* use the resolved A records, but for cross-network
robustness should prefer the explicit `ips=` field in the TXT record
(see below).

### TXT record schema (v=1)

Every field is a UTF-8 string. Optional fields are **omitted** when
unset, not emitted with empty values — the companion must distinguish
"not specified" from "empty string".

| Field | Required? | Example | Semantics |
|---|---|---|---|
| `v` | yes | `1` | Schema version. Companion **must** check this and fail closed on unknown majors. |
| `host` | yes | `dev-laptop.local` | Display name. Same value `socket.gethostname()` returns on the daemon's machine. |
| `user` | yes | `balexand` | Display label. Local user running the daemon. |
| `holo_pid` | yes | `64834` | Daemon PID. Useful for "kill session" UI features. |
| `holo_version` | yes | `0.1.0a18` | Daemon version. Companion can compat-gate features. |
| `started` | yes | `1777924703` | Unix epoch seconds at daemon startup. Sort order for the UI. |
| `cwd` | yes | `/Users/balexand/git/holo` | Working directory when the daemon launched. Project context. |
| `ips` | yes (when any IP is locally available) | `192.168.1.106,10.0.0.5` | Comma-separated IPv4 addresses the daemon believes it's reachable on. **Preferred over A-record resolution** for cross-network UIs. |
| `session` | optional | `claude-1` | Logical session id supplied via `--announce-session`. **Use this to key the companion's session list when present.** |
| `ssh_user` | optional | `balexand` | Username for `ssh <ssh_user>@<host>`. If absent, fall back to `user`. |
| `tmux_session` | optional | `claude-1` | Tmux session name (`#S`) — argument to `tmux attach -t`. Auto-detected when daemon runs inside `$TMUX`. |
| `tmux_window` | optional | `0` | Tmux window (`#W`) for precise targeting. Optional even when `tmux_session` is present. |
| `caps_port` | optional¹ | `49597` | TCP port of the host's capabilities HTTP endpoint (`/capabilities`). Companion fetches `http://<one of ips>:<caps_port>/capabilities` to read the host's hardware/software/package inventory. |
| `caps_token` | optional¹ | `r4nDom-base64url-string` | Auth token for `X-Holo-Caps-Token` request header. **Must be sent on every `/capabilities` request** — wrong/missing token returns `401`. |

¹ `caps_port` and `caps_token` are either **both present or both absent**; the daemon refuses to advertise one without the other. Absence means the host did not opt into the capabilities endpoint via `--announce-capabilities`.

#### Field budget

mDNS TXT records have a soft cap of ~400 bytes total. The current
schema fits comfortably. If we add fields in v2 and need to compress,
we'll either rev `v=` or fold low-signal fields together — the
companion should not assume any fixed byte budget.

### TTL and Goodbye packets

The daemon's announcer emits a TTL=0 "Goodbye" packet on graceful
shutdown (SIGINT, SIGTERM, normal MCP-client disconnect). The companion
will see an `Rmv` event within ~100 ms.

If the daemon is killed with SIGKILL or its host loses network, no
Goodbye is sent and the entry stays in the cache until the natural TTL
expires (~75 s). The companion **must not** rely on `Rmv` events as the
only signal that a session is gone — implement a "last seen" timestamp
and treat sessions as stale after a configurable timeout (suggest
2× the mDNS TTL).

---

## 3. Discovery flow

```
1. Subscribe to "_holo-session._tcp.local."
2. For each Add event:
     - Read TXT record
     - Verify v == "1" (fail closed on unknown major)
     - Build a Session{instance, host, user, ssh_user?, session?,
                      tmux_session?, tmux_window?, ips, cwd, ...}
     - If session_name (`session=` field) is set, key by that.
       Otherwise key by instance label.
     - Insert/update in the companion's session model
3. For each Rmv event:
     - Drop the matching key from the model
4. Background: stale-sweep every N seconds; drop sessions whose
   "last seen" TXT update is older than 2× the cache TTL.
```

Any half-decent zeroconf library will hand these events to you
directly. Examples:

| Language | Library | Notes |
|---|---|---|
| Rust | `mdns-sd` (MIT) | Clean license, sync + async APIs |
| Swift | `NetServiceBrowser` (in `Network`/`Foundation`) | Native, no deps |
| Node.js | `multicast-dns` (MIT) | Low-level; or `bonjour-service` for higher-level |
| Python | `python-zeroconf` (LGPL 2.1+) | Same library holo uses |
| Go | `github.com/grandcat/zeroconf` | Stable, MIT |

---

## 3a. Capabilities HTTP endpoint (optional)

When a daemon is launched with `--announce-capabilities`, it spins up
a small HTTP server alongside the announce broadcast and advertises
the port + auth token in the TXT record (`caps_port`, `caps_token`).
The companion uses this to discover what the host *can do* — hardware
specs, installed software, installed packages — so an agent can route
tasks to the right machine ("send transcription to the M4, not the
M1"; "find a host with Chrome Canary installed").

### Request

```
GET http://<host-ip>:<caps_port>/capabilities
X-Holo-Caps-Token: <caps_token from TXT>
```

The host IP comes from the broadcast `ips` field (preferred) or the A
records. The companion **must** send the auth header — there is no
unauthenticated path to capabilities data. Wrong/missing token returns
`401 Unauthorized` with `{"error": "unauthorized"}`.

There's also a `GET /healthz` endpoint that returns `{"status": "ok"}`
with no auth required, suitable for liveness checks.

### Threat model and CORS

The capabilities server binds `0.0.0.0:<random-port>` so any host on
the LAN can reach it. We assume the LAN is mostly trusted: anyone who
can already snoop the mDNS broadcast has the auth token.

The server is hardened against ONE specific scenario: a random web
origin (`https://evil.com`) trying to fingerprint the host via
cross-origin `fetch()`. Two defenses combine to block this:

1. **Custom auth header.** The required `X-Holo-Caps-Token` is not on
   the [CORS-safelisted request-headers list][cors-safelist], so any
   `fetch()` carrying it triggers a CORS preflight.
2. **No `Access-Control-Allow-*` headers.** The server emits none, so
   the preflight fails — the browser never fires the actual request.

[cors-safelist]: https://developer.mozilla.org/en-US/docs/Glossary/CORS-safelisted_request_header

This stops the browser path; it does **not** stop a same-LAN attacker
who can read the TXT record. Don't put secrets in the capabilities
response that you wouldn't put in the TXT record itself.

### Response shape (schema 2)

```jsonc
{
  "schema": 2,                                // bump on incompatible changes
  "host": {
    "os": "darwin",                           // darwin | linux | windows
    "os_version": "14.5",                     // marketing version (sw_vers on macOS)
    "arch": "arm64",                          // arm64 | x86_64 | amd64
    "cpu_model": "Apple M4 Pro",              // sysctl machdep.cpu.brand_string
    "cores": 14,                              // logical cores (os.cpu_count)
    "ram_gb": 36.0                            // total physical RAM
  },
  "applications": {                           // platform-native catalog
    "Google Chrome": {
      "path":      "/Applications/Google Chrome.app",
      "version":   "147.0.7727.138",          // CFBundleShortVersionString
      "bundle_id": "com.google.Chrome"        // CFBundleIdentifier
    },
    "Firefox": {
      "path":      "/Applications/Firefox.app",
      "version":   "125.0.3",
      "bundle_id": "org.mozilla.firefox"
    }
    // Windows entries also include `publisher` and `install_date`.
    // Linux: applications dict is empty (use `packages.*`).
  },
  "packages": {                               // per-manager arrays — every
                                              // manager whose binary is on
                                              // PATH was queried
    "brew": [
      {"name": "ffmpeg",  "version": "7.0.1"},
      {"name": "whisper", "version": "1.7.0"}
    ],
    "pip":   [...],
    "cargo": [...],
    "npm":   [...]
  },
  "generated_at": 1714900000                  // unix epoch when collected
}
```

**Auto-discovery, no opt-in.** When `--announce-capabilities` is set
on the daemon, the probe runs every supported package manager whose
binary is present:

| Layer | Members |
|---|---|
| macOS third-party | `brew`, `port` |
| Linux OS-supplied | `apt` (via `dpkg-query`), `dnf`/`yum`/`rpm` (via `rpm -qa`), `pacman` |
| Linux third-party | `snap`, `flatpak`, `brew` (linuxbrew) |
| Windows | `winget`, `choco`, `scoop` |
| Cross-platform language-level | `pip`, `pipx`, `cargo`, `npm` (global), `gem`, `conda` (base env) |

Aliases collapse onto canonical keys: `dpkg` → `apt`; `dnf`/`yum` → `rpm`.

**Applications catalog** is platform-specific:
- **macOS:** walk `/Applications`, `/Applications/Utilities`, `/System/Applications`, `~/Applications`, then `mdfind 'kMDItemKind == "Application"'` to pick up bundles outside those dirs. Each entry's `version` and `bundle_id` are read from `Contents/Info.plist` (`CFBundleShortVersionString` → falls back to `CFBundleVersion`; `CFBundleIdentifier`). Both fields are optional — omitted if the plist is unreadable. Private OS agents under `/System/Library/`, `/Library/`, `/usr/libexec/` are filtered out (hundreds of internal helper apps that aren't user-facing).
- **Windows:** read the `HKLM\…\Uninstall` and `HKCU\…\Uninstall` registry keys (canonical "installed programs" list). Each entry includes `path` (from `InstallLocation`), `version` (from `DisplayVersion`), `publisher` (from `Publisher`), and `install_date` (from `InstallDate`, typically `YYYYMMDD`) when registry-supplied. Any field missing from the registry key is omitted from the entry.
- **Linux:** empty — desktop apps come via `packages.snap` / `packages.flatpak` / `packages.apt` etc.

All fields beyond `path` are optional — agents should treat absence as "not known" rather than "not installed".

The probe result is cached server-side for ~60 s — companions can
poll without driving repeated `brew list` / `pip list` invocations
on the host.

### Schema versioning

`schema` follows the same fail-closed rule as the mDNS `v` field: the
companion **must** drop the response if `schema` is greater than the
version it understands. Additive new fields within the current major
are safe and must not break older companions.

**Migration: schema 1 → 2.** The old `software` field (a curated
`shutil.which` whitelist controlled by `--probe-software`) was removed.
PATH-walking proved wrong on Linux, where `/usr/bin` holds both
OS-baseline binaries AND apt-installed software — filtering one would
lose the other. Trusting the package managers as source of truth solves
this. The `applications` field replaces what `software` covered for
macOS GUI apps. Callers querying "is whisper installed?" should now
scan `applications` keys + `packages.*[].name` arrays.

---

## 4. Connection flow

The companion's "open this session" action is conceptually:

```
ssh ${ssh_user}@${ip_or_host} -t "tmux attach -t '${tmux_session}'"
```

Concrete steps:

1. **Choose a host string.** Prefer the first reachable address from
   `ips=` (split on `,`). Fall back to `host` if `ips=` is empty or all
   IPs are unreachable. Do **not** rely solely on `<host>.local`
   resolution — it's flaky on VPNs, corporate networks, and Docker
   bridges.
2. **Choose an SSH user.** Use `ssh_user` if set; else `user`.
3. **Choose a tmux target.** Use `tmux_session` if set; else fall back
   to opening a plain SSH shell (no `-t tmux attach`).
4. **Spawn a terminal** with the constructed command. On macOS this is
   typically `Terminal.app` or `iTerm.app` via `open` / AppleScript.

Authentication is the user's SSH agent / keys; the companion never
prompts for a password and never stores credentials.

### Multiple sessions on one host

If the user runs `holo mcp --announce` twice on the same machine
(different `--announce-session` values), the companion will see two
distinct mDNS instances with the same `host` and different `session=`
fields. Both should appear in the UI as separate rows.

### Same session name across hosts

Nothing prevents two different machines from advertising
`session=claude-1`. The companion should disambiguate in the UI by
also showing `host` / `user`. Internally, key sessions by
`(host, session_name)` or by instance label, not by `session_name`
alone.

---

## 5. Caveats and gotchas

- **mDNS is link-local.** No discovery across NAT, no discovery across
  most VPN topologies. The companion's primary failure mode will be
  "I can SSH to that host but it doesn't show up in the list."
- **Goodbye packets are not guaranteed.** SIGKILL, network partitions,
  and crash-loops produce ghost entries. Implement a stale sweep.
- **Hostname truncation.** GitHub Actions hostnames are ~60 bytes; the
  daemon truncates the instance label to fit. Don't expect the
  instance label to round-trip the full hostname.
- **Schema versioning.** This spec is `v=1`. Future versions may
  rename or drop fields. The companion **must** fail closed on a
  major version bump; minor adds are backwards-compatible.
- **No authentication is broadcast.** Don't add it. SSH key
  fingerprints, tokens, passwords — none of these belong in a TXT
  record visible to anyone on the LAN.
- **No auto-connect.** The user must explicitly choose to connect to
  a discovered session. Don't auto-spawn SSH on discovery — a
  malicious LAN peer could broadcast a fake session and hope the
  companion connects to it. (SSH key auth limits the blast radius
  here, but explicit user action is the right default.)

---

## 6. Out of scope for this spec

The following are product decisions the companion team should make
independently. The spec deliberately doesn't pin them:

- **UI framework** (Tauri, Electron, native Swift, terminal UI…).
- **Droid model** — what does the companion DO with a connection
  beyond launching a terminal? (Read-only mirror of the Claude
  conversation? Bidirectional control? Background workers?)
- **Multi-session orchestration** — can one companion drive several
  Claude sessions simultaneously?
- **Persistence** — does the companion remember sessions across
  restarts, or rebuild from mDNS each time?
- **Cross-LAN discovery** — fallback to a shared registry service
  when mDNS isn't viable. This is the
  [`holo connect` / `holo mcp --listen` flow](./cross-host.md);
  the companion can drive that too, but it's a different transport
  with different UX.

---

## 7. Validation tools

For implementers, these macOS commands let you observe the broadcast
without writing any code:

```bash
# List all live holo sessions
dns-sd -B _holo-session._tcp local

# Dump full TXT + SRV for one instance
dns-sd -L "<instance-label>" _holo-session._tcp local

# Resolve a host's A record
dns-sd -G v4 <host>.local
```

On Linux:

```bash
avahi-browse -r _holo-session._tcp
```

To run a daemon for testing:

```bash
holo mcp --listen 7777 --no-bookmarklet --announce \
  --announce-session test-1 \
  --announce-user $USER \
  --announce-ssh-user $USER
```

`--listen 7777` makes the daemon stay alive without an MCP client
attached (otherwise stdio mode exits when stdin closes).
`--no-bookmarklet` skips the WebSocket server, which isn't relevant
to companion testing.

---

## 8. Open questions (companion ↔ holo)

These are deliberate open ends — answer when implementing:

1. Does the companion need a way to **request** a holo session? E.g.
   the desktop UI says "open a new Claude session on host X" and
   something on host X spawns `holo mcp --announce`. Today the user
   does this manually inside their tmux session.
2. Should the broadcast carry the **MCP transport** (stdio vs `--listen
   PORT`) so the companion can offer "connect MCP-side directly"
   instead of "ssh into tmux"? Currently inferable from `port` (0 =
   stdio, nonzero = listen) but not flagged explicitly.
3. Is there a future `_holo-session._tcp.local.` field for **conversation
   id** or **last activity** that the companion would use to surface
   "this session has been idle for 4 hours"?

These don't need to be solved before companion v1, but bookmark them.
