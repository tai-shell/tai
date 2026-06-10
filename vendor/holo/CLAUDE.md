# CLAUDE.md — holo project guide

`holo` is a browser + screen automation primitive for AI agents. A CLI agent (Claude Code, Codex, etc.) drives the user's already-signed-in browser via a same-origin **bookmarklet** that talks to a local **daemon**. No extension, no headless browser, no credential storage.

Repo layout:
- `src/holo/` — Python daemon (macOS-first; Windows shim exists but is partial)
- `bookmarklet/` — JS payload built with esbuild, run as `javascript:` URL on any tab
- `tests/` — pytest for daemon, `bookmarklet/test/` for JS

## Architecture: two transports, one channel

The daemon ↔ bookmarklet channel has **two transports**, chosen at runtime:

1. **WebSocket (preferred)** — bookmarklet opens a cross-origin popup pointing at `http://127.0.0.1:<port>/popup.html` (served by the daemon's `WSServer`). The popup connects back via WS, handshakes with `{sid, token}`, then relays commands/results between daemon and host page via `postMessage`. Low latency, no focus stealing.
2. **QR / clipboard fallback** — daemon writes the command to the clipboard, focuses the bookmarklet's `about:blank` popup, sends Cmd+V; the page replies by rendering a QR code in a `<canvas>` and the daemon decodes it via macOS Vision framework.

Both run side-by-side; the daemon prefers WS but the QR poller stays live so a failed/refused WS attach silently falls back. On strict sites (`tai.sh` has `default-src 'none'` + `Cross-Origin-Opener-Policy: same-origin`) the WS popup detects a severed `window.opener` and refuses to attach, and we stay on QR.

### Two-popup design (important, non-obvious)
The bookmarklet keeps its **original `about:blank` popup** (for QR rendering / paste target) and opens a **second cross-origin popup** for the WS client. The WS popup reaches the host page via `window.opener.opener` (WS popup → about:blank popup → host). Don't replace the original popup with `location.replace` — that breaks QR fallback.

### Stealth QR (`--hide-qr`)
The popup paints reply QRs in two near-identical greens (`rgb(120,200,120)` light, `rgb(124,200,120)` dark — only 4-unit delta on the red channel). Humans and external phone cameras can't decode them. The daemon thresholds the captured framebuffer's red channel against `STEALTH_PIVOT_R = 122` to reconstruct a real B/W QR before handing it to Vision. Done via raw pixel access through `CGBitmapContext` + `bytearray` — **not** Core Image, because CI filters operate in linear-light by default and an sRGB pivot value gives wrong results without explicit color-space handling.

## Key files

| File | Role |
|---|---|
| `src/holo/daemon.py` | Single-process owner of `WSServer` + `ChannelRegistry`. `daemon.calibrate()` returns one `Channel` per tab. |
| `src/holo/channel.py` | Per-tab command channel. Owns title-poll calibration, QR poller, optional WS attachment. `send_command` picks transport. |
| `src/holo/registry.py` | Thread-safe `dict[sid, Channel]`. |
| `src/holo/ws_server.py` | websockets sync server on `127.0.0.1:<random>`. Serves `popup.html` + `framing.js` (HTTP) and routes WS handshakes to channels. |
| `src/holo/_macos.py` | AppKit/Quartz/Vision helpers — process activation (osascript fallback for Sonoma+), Cmd+V via System Events, window-pixel capture, QR decode, stealth-QR amplification. |
| `src/holo/cli.py` | `holo demo`, `holo doctor`. `--manual` and `--hide-qr` flags. |
| `src/holo/static/popup.html` | Daemon-served WS-client popup body. Reads `sid/token/parentOrigin` from URL fragment. Detects COOP severance and refuses on no-opener. |
| `bookmarklet/core.js` | Bookmarklet entrypoint — creates `about:blank` popup, registers host listener, opens WS popup on handshake, renders QR replies. |
| `bookmarklet/framing.js` | Title/QR framing protocol (chunk + ack). Mirrored in `src/holo/static/framing.js` for the popup's ES-module import. |

## Build / test commands

Python:
- `uv sync` — install deps incl. dev extras
- `uv run pytest` — run all daemon tests
- `uv run ruff check src tests` — lint
- `uv run holo demo` / `uv run holo demo --manual` / `uv run holo demo --hide-qr` — end-to-end smoke

Bookmarklet:
- `cd bookmarklet && npm install && npm run build` — produces `bookmarklet/dist/install.html` and bundled JS
- `npm test` — JS unit tests

After editing `bookmarklet/core.js` you must rebuild **and** re-drag the bookmarklet from `dist/install.html`. The bookmarklet's `install()` short-circuits when its popup already exists, so close the popup before re-installing.

## Conventions / gotchas

- **macOS Sonoma cross-app activation is restricted.** `NSRunningApplication.activateWithOptions_` silently fails from non-foreground processes. We always belt-and-suspenders with `osascript -e 'tell application "X" to activate'`. See `_macos.activate_pid`.
- **`pyautogui.hotkey('command','v')` doesn't reach Chrome popups reliably.** Use `keystroke_paste` (System Events via osascript) instead.
- **WindowServer truncates window titles >~70 chars.** That's why replies use the QR/canvas channel, not titles.
- **CSP varies a lot.** `tai.sh` is strict (`default-src 'none'`, only `ws://localhost:7080` allowed in `connect-src`); github.com / example.com are permissive. Don't assume connect-src or frame-src.
- **`websockets` sync `Headers` is multi-dict.** `__setitem__` appends; use `del response.headers["Content-Type"]` before setting if you need to overwrite.
- **The QR poller is opportunistic, not load-bearing.** Channels for which WS never lands stay on QR indefinitely — that's by design.
- **Per-process token + per-channel sid** — WS handshake validates `{sid, token}` against the registry. Don't leak the token.

## Branching / PR flow

- Feature branches off `main`. PRs squash-merged. Branch deleted on merge.
- Recent merged work:
  - **#19** — Phase 0 QR pivot (replies via canvas + Vision decode, escapes the title length limit)
  - **#20** — Phase 1 WebSocket transport + stealth-QR fallback (this is what `main` is now at: `8c5b6d5`)

## Phase status (from README)

- [x] Phase 0 — primitive layer (channel, framing, bookmarklet)
- [x] Phase 1 — agent surface (MCP server `holo mcp` over stdio; WS transport + stealth-QR fallback)
- [x] Phase 2 — cross-host bridge (`holo mcp-remote` spawn-per-connection stdio proxy + `holo mcp --listen PORT` / `holo connect HOST:PORT` persistent-daemon TCP transport — see `docs/cross-host.md`)
- [ ] Phase 3 — opt-in CDP adapter

## MCP surface (Phase 1)

`src/holo/mcp_server.py` exposes a `FastMCP` server with the channel + screen tools (`calibrate`, `list_channels`, `drop_channel`, `ping`, `read_global`, `send_command`, `bookmarklet_query`, plus `app_activate`, `screen_*` when `--screen` is on, plus `browser_*` AppleScript ops). `HoloMCPServer` owns one lazily-constructed `Daemon` and translates `CalibrationError` / `CommandError` into MCP-style runtime errors.

Four flags tailor the surface:
- `--screen` (kwarg `enable_screen`) — registers SikuliX-backed tools (`screen_*`, `app_activate`, `ui_template_*`). Off by default; opt-in keeps the JVM cost off the table for browser-only agents.
- `--no-bookmarklet` (kwarg `no_bookmarklet`) — skips the WSServer entirely and drops the seven channel-dependent tool registrations (`calibrate`, `list_channels`, `drop_channel`, `ping`, `read_global`, `send_command`, `bookmarklet_query`). Suits agents that never touch the bookmarklet (Slack-only orchestrator, AppleScript-only nav). `Daemon.calibrate()` raises in this mode; channel methods on `HoloMCPServer` still exist defensively but the tools aren't exposed.
- `--no-browser` (kwarg `no_browser`) — drops the eleven `browser_*` AppleScript tools (`browser_navigate` / `_new_tab` / `_close_active_tab` / `_activate_tab` / `_list_tabs` / `_read_active_url` / `_read_active_title` / `_reload` / `_back` / `_forward` / `_execute_js`). Used by the input-only peer in the split-input-proxy topology where Chrome control is irrelevant.
- `--input-proxy HOST:PORT` / `--remote-input HOST:PORT` (kwarg `input_proxy=(host, port)`, aliased flags) — routes the five input ops (`screen_click`, `screen_key`, `screen_type`, `screen_scroll`, `app_activate`) to a remote `holo mcp --listen` instead of the local SikuliX bridge. See **Split-machine proxy** below.
- `--remote-screen HOST:PORT` (kwarg `remote_screen=(host, port)`) — symmetric to `--input-proxy` for capture: routes `screen_shot`, `screen_find_image`, `screen_user_capture`, and `ui_template_find`/`ui_template_capture`'s capture leg to a remote `holo mcp --listen` instead of the local bridge. When both proxies target the same host, holo shares one MCP connection. See **Split-machine proxy** below.

Plus separate orthogonal capabilities:
- `--announce` (kwargs `announce`, `announce_session`, `announce_user`, `announce_ssh_user`) — broadcasts an mDNS service record (`_holo-session._tcp.local.`) so a companion desktop app on the same LAN can discover live sessions. See **Session announcement** below.
- `--announce-capabilities` (kwarg `announce_capabilities`) — requires `--announce`. Stands up a token-auth HTTP endpoint (`GET /capabilities`) carrying hardware + applications + packages inventory; advertises `caps_port` + `caps_token` in the TXT record so an agent can route tasks by host capability. Everything is auto-discovered per platform — no per-probe flags. See **Capabilities endpoint** below.

Internal naming kept as-is: `BridgeClient`, `daemon.bridge`, `_require_bridge` describe the JVM bridge implementation accurately. Only the user-facing flag (`--bridge` → `--screen`), the matching kwarg (`use_bridge` → `enable_screen`), and two CLI subcommands (`holo bridge` → `holo screen`, `holo install-bridge` → `holo install-screen`) were renamed.

Two transports:
- `holo mcp` — stdio (default; for `claude mcp add` spawning per session)
- `holo mcp --listen PORT` — TCP on 127.0.0.1:PORT, single concurrent connection, magic-prefix handshake (`HOLO/1\n`) required before MCP traffic. Daemon state persists across reconnects. Used with `holo connect HOST:PORT` (a stdio↔TCP bridge that injects the prefix) on the remote side of an SSH-tunnelled `mcp-remote`. Solves the macOS-TCC-on-SSH problem: the listener runs in a tmux session that has Screen Recording permission; SSH-spawned processes wouldn't.

When the registry is non-empty, `calibrate` fast-paths to the most recent channel — cross-host setups depend on this so the human can calibrate locally on the daemon's machine and the remote agent inherits the channel.

Both transports must keep stdout clean — diagnostics go to stderr only.

## Browser ops (AppleScript adapter)

`src/holo/browser_chrome.py` wraps Chrome's AppleScript dictionary as `browser_*` MCP tools (`browser_navigate`, `browser_new_tab`, `browser_list_tabs`, `browser_activate_tab`, `browser_close_active_tab`, `browser_read_active_*`, `browser_reload`, `browser_back`, `browser_forward`, `browser_execute_js`). These bypass the SikuliX keystroke layer entirely — no `app_activate` race, no focus beeps, no Accessibility permission needed for keyboard injection. Only Automation permission for the launching Terminal → Google Chrome.

**Use `browser_*` for any Chrome navigation.** SikuliX's `screen_key`/`screen_type` should be reserved for non-browser apps (terminals, native dialogs, canvas content).

For arbitrary DOM reads, prefer `browser_execute_js` (AppleScript) — it has no CSP constraint because it runs on Chrome's main world via Apple Events, not via a script the page can block. It needs Chrome → View → Developer → **Allow JavaScript from Apple Events** turned on; when that toggle is off, `JavaScriptNotAuthorized` surfaces a message naming the menu item and points the agent at `bookmarklet_query` (`query_selector` / `query_selector_all` ops in `bookmarklet/dispatch.js`) as the CSP-safe fallback. The bookmarklet path stays useful even when the AppleScript path is available — it doesn't need the toggle and works on any tab where the bookmarklet is calibrated.

macOS-only. Linux/Windows browser ops will land via the Phase 3 CDP adapter — Chrome 136+ blocks `--remote-debugging-port` on the default profile, so CDP requires a profile-switching dance that defeats holo's auth-piggyback pitch on macOS, but it's the right path for non-macOS.

## Session announcement (mDNS / DNS-SD)

`src/holo/announce.py` broadcasts a `_holo-session._tcp.local.` service record so a companion desktop app on the same LAN can discover live holo sessions and build "droid" connections (SSH + tmux attach) to reach them. **No authentication material is broadcast** — credentials live in the user's SSH config / agent. The TXT record carries only metadata.

Implementation: `python-zeroconf` (LGPL 2.1+, pure-Python). It speaks the multicast protocol directly via raw sockets — Avahi (Linux) and Bonjour (Windows) are NOT required. Cross-platform out of the box.

TXT schema (v=1):
| Field | Always? | Source |
|---|---|---|
| `v` | yes | constant `1` (schema version) |
| `host` | yes | `socket.gethostname()` |
| `user` | yes | `--announce-user` flag, else `getpass.getuser()` |
| `holo_pid` | yes | `os.getpid()` |
| `holo_version` | yes | `holo.__version__` |
| `started` | yes | unix epoch at startup |
| `cwd` | yes | `os.getcwd()` |
| `session` | optional | `--announce-session` only |
| `ssh_user` | optional | `--announce-ssh-user` only |
| `tmux_session` / `tmux_window` | when in tmux | `tmux display-message -p '#S' / '#W'` |

**Omit-when-not-specified rule.** If a flag isn't passed, the field is omitted entirely (not emitted as empty). The desktop UI distinguishes "unset" from "set to empty" this way.

`HoloAnnouncer` is constructed by `HoloMCPServer.__init__` when `announce=True` and stopped in `shutdown()`. Failures during `start()` (no network, multicast disabled, etc.) are logged to stderr and the server continues without broadcasting — announce is best-effort, not load-bearing.

Smoke verification on macOS:
```bash
holo mcp --announce --announce-session test --no-bookmarklet &
dns-sd -B _holo-session._tcp local
dns-sd -L <instance> _holo-session._tcp local   # full TXT dump
```

The advertised SRV port is `0` in stdio mode and the `--listen` port in TCP mode. The desktop companion uses TXT data + SSH config for the actual connection — the SRV port is metadata, not an endpoint to dial directly.

## Session discovery (`holo discover`)

`src/holo/discover.py` is the in-tree consumer of the announce contract. Three output modes:

| Mode | Use |
|---|---|
| `holo discover --json [--wait SECS]` | One-shot snapshot, JSON array on stdout, exit. Default browse window 3 s. |
| `holo discover --tail [--stale-after SECS]` | Long-running JSONL event stream (`{"type":"add"\|"remove"\|"update", ...}`). Stale sweep at 2× cache TTL. |
| `holo discover --serve PORT [--cors-origin ...]` | HTTP + WebSocket server (default port `7082`). `GET /sessions`, `GET /healthz`, `WS /events`. |

The R2D2 desktop SPA on the user's laptop hits `--serve 7082`. The HTTP layer is Starlette (lifespan ctx mgr, no `on_startup`/`on_shutdown` — those were removed in Starlette 1.0). uvicorn drives the loop. Default CORS allow-list is `http://localhost:8888,https://app-dev.tai.sh`; override with `--cors-origin A,B,C`.

**Schema validation:** `parse_txt` shares field-name constants and the `REQUIRED_FIELDS` / `INT_FIELDS` tuples with `announce.py`. A TXT with `v != "1"` or missing required fields is logged at WARNING and dropped — fail closed, per spec §2.4.

**Thread model:** `SessionStore` uses `threading.RLock`. Zeroconf callbacks run on its own thread; the stale-sweep is a daemon thread; `--serve`'s WS handler hops events from the zeroconf thread to the asyncio loop via `loop.call_soon_threadsafe`. Subscribers are called *while the lock is held*, so they must not block.

**Goodbye vs. stale sweep:** if the announcer exits cleanly (SIGINT / SIGTERM), zeroconf delivers a `Rmv` event ~100 ms later → `remove` event downstream. If the host crashes / SIGKILL'd, no Goodbye fires; the stale sweep drops the entry after `--stale-after` seconds (default 150 = 2× zeroconf's 75 s TTL).

**`/healthz`** returns `{status, interfaces, zt_present}`. `zt_present` is a heuristic — checks for any interface name starting with `zt`. ZeroTier on macOS/Linux uses that prefix. Windows ZT names interfaces by description, not prefix; the heuristic returns false there but doesn't break anything.

## Capabilities endpoint (`--announce-capabilities`)

`src/holo/capabilities.py` + `src/holo/capabilities_server.py` together stand up an opt-in HTTP endpoint that exposes the host's hardware + applications + packages inventory so an agent can route tasks to the most-capable host (M4 vs M1 transcription, "find a host with Chrome Canary or whisper installed", etc.). Schema 2.

Wiring: `HoloMCPServer.__init__` constructs `CapabilitiesProbe` + `CapabilitiesServer` *before* `HoloAnnouncer` so the bound port + auth token make it into the TXT record. Server runs uvicorn on a daemon thread bound to `0.0.0.0:<random>`. `_caps_server.actual_port` blocks until the listener is up (5 s timeout). On shutdown the announcer stops first (Goodbye), then the caps server, then the daemon — minimizes the window where a discoverer sees the broadcast but hits connection-refused on the URL.

**Endpoints:**
| Path | Auth | Notes |
|---|---|---|
| `GET /capabilities` | `X-Holo-Caps-Token: <hex>` (constant-time compare) | JSON snapshot — see spec §3a for shape. Probe results cached 60 s. |
| `GET /healthz` | none | Returns `{"status": "ok"}` — intentionally minimal so unauthenticated LAN scanners can't fingerprint holo hosts. |

**Browser-block defenses (two-layer):**
1. Custom required header `X-Holo-Caps-Token`. Not on the [CORS-safelisted request-headers list](https://developer.mozilla.org/en-US/docs/Glossary/CORS-safelisted_request_header) so any cross-origin `fetch()` with it triggers a CORS preflight.
2. No `Access-Control-Allow-*` headers in any response. Preflight fails → browser never fires the actual request.

This stops random web origins from fingerprinting the host. It does **not** stop a same-LAN attacker who can read the mDNS broadcast — they get the token. Threat model: web origins, not local LAN.

**Probes — all auto, no flags:**
- **Hardware** (always when capabilities is on): `os`, `os_version` (`sw_vers -productVersion` on macOS to dodge the libSystem 10.16 lie under Anaconda Python), `arch`, `cpu_model`, `cores`, `ram_gb`. Cross-platform (macOS sysctl, Linux /proc, Windows ctypes).
- **Applications**: macOS walks `/Applications`, `/Applications/Utilities`, `/System/Applications`, `~/Applications`, then merges `mdfind 'kMDItemKind == "Application"'` results — filters out private OS agents under `/System/Library/`, `/Library/`, `/usr/libexec/` (hundreds of internal helper apps). For each `.app`, `version` + `bundle_id` are read from `Contents/Info.plist` via `plistlib` (~1 ms per app, ~120 ms total on a typical Mac). Windows reads HKLM + HKCU `…\Uninstall` registry keys; entries carry `path`, `version`, `publisher`, `install_date`. Linux empty (apps come via packages.*). All fields beyond `path` are optional — omitted when the source can't supply them.
- **Packages**: every supported manager whose binary is on PATH gets queried automatically. Members: `brew`, `port`, `apt` (via `dpkg-query`), `dnf`/`yum`/`rpm` (via `rpm -qa`), `pacman`, `snap`, `flatpak`, `winget`, `choco`, `scoop`, plus language-level `pip`, `pipx`, `cargo`, `npm` (-g), `gem`, `conda` (base env). Aliases collapse to canonical keys: `dpkg` → `apt`; `dnf`/`yum` → `rpm`.

**Why no PATH walk?** The previous design walked `$PATH` for the `software` field, skipping system dirs. On Linux that's broken: `/bin` is a symlink to `/usr/bin`, and `/usr/bin` holds both OS-baseline binaries AND apt-installed software. Skipping it loses real signal; not skipping it floods the response with `ls` / `cat` / `grep` noise. Trusting the package managers as source of truth dodges the problem entirely — apt-installed `ffmpeg` shows up under `packages.apt` regardless of where on PATH it lives.

Token: `secrets.token_urlsafe(32)`, generated per-process. Don't reuse across runs.

Smoke verification:
```bash
holo mcp --announce --announce-capabilities --no-bookmarklet &
dns-sd -L <instance> _holo-session._tcp local | grep caps_
# then:
curl -H "X-Holo-Caps-Token: <token from above>" http://127.0.0.1:<caps_port>/capabilities | jq .
```

**MCP tool surface for the read side** (always exposed, no `--bookmarklet`/`--screen` dependency): `holo_discover_sessions(wait_s=0)` returns sessions from a continuously-running mDNS cache — instant, no per-call browse delay; `holo_fetch_capabilities(instance, timeout_s=5)` accepts an instance label / session / host (matched in that order via `_match_session`), reads the target from the cache, picks the first reachable IP from the broadcast list, and returns `{instance, host, session, ip_used, capabilities}`. Both are how an agent connected to one holo session does capability-aware routing — without them, `--announce-capabilities` is invisible to the agent and only useful via `holo discover` + `curl` from the user's shell.

**Persistent discovery cache.** `HoloMCPServer.__init__` always starts a `DiscoverHandle` (zeroconf browser + `SessionStore` + stale sweeper, always-on, regardless of `--announce`) so the two read-side tools answer instantly. The cache populates over the first ~2-3 s of mDNS solicitation; agents that just spawned a daemon and want to immediately query it can pass `wait_s=1.5` to `holo_discover_sessions` as a grace period. mDNS startup failure (no network, multicast disabled) is logged and downgrades to "discover_sessions returns empty / fetch_capabilities raises" — never kills the MCP server.

## Split-machine proxy (`--input-proxy` / `--remote-screen`)

Corporate macOS lockdowns commonly disable `java.awt.Robot` / `CGEvent`-based event injection (Accessibility TCC blocked by MDM profile), and the same MDM often also denies Screen Recording to specific apps like Claude Code (which becomes the *responsible process* in TCC's launch-chain attribution for anything Claude Code spawns). Two independent proxies let A offload either or both halves of the SikuliX surface to a peer that DOES have the TCC grants:

- `--input-proxy HOST:PORT` (alias `--remote-input`) — `click` / `key` / `type` / `scroll` / `activate` go to the peer.
- `--remote-screen HOST:PORT` — `screenshot` / `find_image` / `find_image_path` / `user_capture` go to the peer.

Both flags accepted together. When both target the same host, the Daemon constructs a single `RemoteHoloBackend` and assigns it to both `_remote_input` and `_remote_screen` (`Daemon._backends` is a `{(host,port): backend}` dedup map). One MCP connection, one connect subprocess, one shutdown.

**Topology — input proxy alone** (capture works locally on A; only input is MDM-blocked):
- **Peer B:** `holo mcp --screen --no-bookmarklet --no-browser --listen 7081`. Stock deployment. macOS Screen Sharing.app on B must be foreground, fullscreen, 1:1 mapping, "pass all keys" enabled so events fire inside the viewer's coordinate space.
- **A:** `holo mcp --input-proxy 192.168.1.50:7081`. A captures locally, A's `_input_target()` proxies clicks to B, B fires them on its display, Screen Sharing.app relays them back to A.

**Topology — both proxies, A becomes a pure orchestrator** (when A's capture is also broken, e.g. Claude Code is the TCC responsible process and lacks Screen Recording):
- **Peer B:** same as above.
- **A:** `holo mcp --input-proxy 192.168.1.50:7081 --remote-screen 192.168.1.50:7081`. Both capture AND input run on B. `daemon.bridge` is never lazy-started on A — neither `_input_target()` nor `_capture_target()` ever falls through to it. A only needs the MCP server + the proxy machinery.

**Routing helpers** (`HoloMCPServer`):
- `_input_target()` → `daemon._remote_input or daemon.bridge`
- `_capture_target()` → `daemon._remote_screen or daemon.bridge`

`ui_template_click` is the cross-cutting compound: find runs through `_capture_target()`, click through `_input_target()`. With both flags pointing at the same peer (shared backend), the entire compound runs on B. With distinct peers, find happens on the screen target and click on the input target — they can legitimately differ.

`RemoteHoloBackend` (`src/holo/remote_backend.py`) is a sync facade over the official `mcp` Python SDK's async `ClientSession`. It spawns `holo connect HOST:PORT` as a child subprocess (existing stdio↔TCP bridge with magic-prefix handshake), wraps stdio in `stdio_client`, runs an asyncio loop on a dedicated daemon thread, and exposes sync methods whose signatures match `BridgeClient`'s — both input and capture. Tool calls go through `asyncio.run_coroutine_threadsafe`. Method names map 1:1 to MCP tool names on the remote (`click` → `screen_click`, `screenshot` → `screen_shot`, `user_capture` → `screen_user_capture`, etc.). FastMCP's `structuredContent` is preferred for return-value unwrapping; falls back to parsing the first text block as JSON; empty results surface as `None` (which `find_image` relies on for "no match"). `isError=true` results raise `RemoteHoloError`. `src/holo/remote_input.py` is a backward-compat shim that re-exports `RemoteHoloBackend` / `RemoteHoloError` under the old names.

`find_image_path` is implemented locally-then-remote: the template file is read from A's disk (the remote's filesystem doesn't have our `~/Library/Caches/holo/templates` tree) and the bytes are sent to the peer as base64 — same wire shape as `find_image`. `user_capture` requires a `screen_user_capture` MCP tool on the peer; the wrapping `ui_template_capture` (drag-rectangle path) routes through it when `--remote-screen` is set so the SikuliX overlay renders on B's display (which, in the Screen-Sharing topology, IS A's screen from the user's viewpoint).

Trust model: the magic-prefix handshake gates connections to B's `--listen` port; same-LAN attackers who can hit the port can drive A's input AND read A's screen through this channel. Don't expose `holo mcp --listen` outside trusted segments. Coordinate space is assumed 1:1 between A's display and B's viewer (Screen Sharing.app default); HiDPI / scaling mismatches would need a translation factor not yet implemented.

## UI template cache (desktop DOM analog)

For UI elements outside any browser tab — Chrome's kebab/bookmarks bar, the dock, menu-bar items, native app buttons — `src/holo/templates.py` + the `ui_template_*` MCP tools provide a persistent name → image cache. Capture an element once (`ui_template_capture` blocks for a SikuliX `Region.userCapture()` rect drag, or accepts an explicit `region` for programmatic stash), then `ui_template_click("kebab", app="chrome")` in future sessions does template matching via `find_image_path` and clicks the center. Avoids re-doing vision each session for stable on-screen elements.

Storage: `~/Library/Caches/holo/templates/<app>/<label>.png` plus `index.json` with metadata (similarity, dimensions, last_used, match_count). `app=None` routes to `_global/`. Override root with `HOLO_TEMPLATE_DIR`. Variants per label support hover/idle/dark-mode states — `_find` walks them in order and returns the first hit; `_click` raises if nothing matches (silent miss is worse than a clear error). New JVM-side handlers `screen.user_capture` and `screen.find_image_path` mirror the existing `screen.shot` / `screen.find_image` but optimize for the cache's "PNGs already on disk" path.
