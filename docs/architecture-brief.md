# Browser Automation for AI Agents — Architecture Brief

**Status:** design proposal · v0.1 · 2026-04-28

## Problem

Today's AI-CLI browser tools (Claude in Chrome, browser-mcp, Playwright wrappers) are built around a Chrome extension or CDP-driven hermetic browser. That shape has four wrong-fitting boundaries:

1. **Co-location.** Agent and browser must share a host. Remote dev boxes and screen-share workflows break.
2. **Browser-only scope.** Sandboxed in the page; can't reach OS dialogs, native auth prompts, terminals, or pixel-rendered canvas/WebGL.
3. **Tab ↔ CLI binding is fragile.** Tab IDs are session-scoped, drift across restarts, lose state on crashes.
4. **Auth ceiling.** Vendor policies cap login/MFA/SSO flows; real services tend to break.

For our concrete use case — testing The A.I. Shell (R2D2) — these aren't inconveniences; the droid windows render to xterm.js → canvas with an empty accessibility tree, so DOM-only tools are *structurally insufficient*.

## Proposal

A two-layer browser-automation primitive with a thin MCP-shaped surface for agents:

- **OS layer** (PyAutoGUI + OpenCV; SikuliX optional) drives mouse, keyboard, screenshots, native windows. Reaches anything on screen — including canvas/WebGL.
- **In-page layer** (fat bookmarklet, ~5 KB inlined) runs same-origin JS inside the user's actual browser, riding their real auth sessions. DOM, IDB, internal globals, programmatic events.
- **Channel** is layered. Page → daemon: `document.title` (universal, doesn't disturb user state). Daemon → page: clipboard-paste with restore (universal, ~100 ms disturbance window). Same-origin localhost bridge available as a perf optimization for origins where we control CSP. All carry the same framing protocol: session ID, sequence + total, checksum, idempotency key, ack/pong, navigation sentinel, base64 envelope.

A small local daemon translates agent tool calls into both layers. The agent sees only the tools.

## Architecture

Two operating modes, selected automatically based on whether a local daemon is watching for the bookmarklet's calibration beacon:

### Local mode (default)

CLI and browser on the same machine. No backend, no infrastructure, no auth.

```
┌─ Developer's machine ──────────────────────────────────┐
│                                                         │
│ ┌─ Terminal: agent (Claude Code, …) ─────┐             │
│ │   MCP stdio                              │             │
│ │     ▼                                    │             │
│ │  ┌─ harness daemon (Python) ──────────┐ │             │
│ │  │ • OS driver                        │ │             │
│ │  │ • title reader / paste writer      │ │             │
│ │  └────┬───────────────────────────────┘ │             │
│ └───────┼─────────────────────────────────┘             │
│         │ window title / clipboard                       │
│         ▼                                                │
│ ┌─ Browser ────────────────────────────────┐            │
│ │ Bookmarklet on active tab                │            │
│ └──────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────┘
```

### Cross-host mode (opt-in)

CLI and browser on different machines. Adds a registry + popup-bridge service.

```
┌─ Host A (CLI) ───┐         ┌─ harness.tai.sh ─────────┐         ┌─ Host B (browser) ──┐
│ harness daemon   │ ──wss──▶│ registry + bridge        │◀──wss── │ Bridge popup        │
│ (signed-cert    │         │  • session registry      │         │ (postMessage with   │
│  auth)           │         │  • account-scoped routing│         │  bookmarklet on    │
└──────────────────┘         └──────────────────────────┘         │  active tab)        │
                                                                   └──────────┬──────────┘
                                                                              │ postMessage
                                                                              ▼
                                                            ┌─ Active tab (any origin) ─┐
                                                            │ Bookmarklet               │
                                                            └────────────────────────────┘
```

The popup is the bridge that lifts the bookmarklet over third-party origins' CSP walls — `window.open` to a permissive origin we own is allowed even when WebSocket to that origin from the host page is blocked.

The framing protocol on the wire is identical in both modes; the daemon just speaks different transports.

## Why this beats existing options

| Pain point | This architecture's answer |
|---|---|
| Co-location | Cross-host mode rides outbound WSS to a registry — works through every NAT, firewall, corporate network. |
| Browser-only scope | OS layer is first-class. Reaches native dialogs, terminal apps, canvas/WebGL. |
| Tab/CLI binding | Sessions have durable IDs; both sides reconnect across tab close, browser restart, network blip. |
| Auth ceiling | Piggybacks the user's real logged-in browser sessions across every origin. |

The **auth-piggyback** property is the wedge. No other browser-MCP solution rides the user's existing sessions. For tests against real services (GitHub releases, AWS Console, Cognito, internal SSO-gated apps), this changes the build-vs-buy math.

## Distribution & developer setup

Local mode is the OSS-friendly path: zero infrastructure, runs on a laptop.

```
$ pipx install harness-mcp                    # or `brew install harness-mcp`
$ harness daemon                               # starts the local watcher
```

Then once in the browser:

1. Visit `https://harness.tai.sh/install` (static page — no auth, hosts the bookmarklet).
2. Drag "🔧 Harness" to the bookmarks bar.
3. Click the bookmark on any normal `http(s)` page. Calibration completes in seconds.

Agent config points at `harness mcp` (stdio). Standard MCP server pattern.

Cross-host mode adds:

- Daemon registers with the registry on startup, presenting an SSH-CA-signed cert issued by the existing tai.sh CA via a one-time browser-assisted device-link flow.
- Browser-side pairing via `https://harness.tai.sh/pair` (or popup-from-bookmarklet) lists the user's registered daemons; auto-pair if exactly one.

## Tradeoffs (what we give up vs. CDP/extension)

- **No structured network interception.** Bookmarklet wraps `fetch`/`XHR` from in-page; can't see anything earlier than first script. CDP/extensions can.
- **Latency floor.** Local mode adds channel round-trip (~100 ms title polling); cross-host adds a network hop. Fine for human-paced UI tests; not for high-frequency automation.
- **One-time bookmark-bar install.** User drags the bookmarklet per browser they want to drive.
- **Synthetic-event detection.** Some anti-bot heuristics flag programmatic clicks. Mitigation: route auth-sensitive steps through the OS layer, which fires *real* OS clicks. (Hidden upside of having both layers.)
- **Wayland on Linux.** Wayland restricts global cursor polling, keystroke injection, and screen capture. Local mode degrades; documented, not papered over.
- **macOS permissions.** Screen Recording + Accessibility require explicit grant. Daemon detects missing permissions and prints exact instructions on first run.

## Phasing

- **Phase 0 ✓** — primitive layer: Python lib + bookmarklet payload + title/clipboard channel + framing protocol.
- **Phase 1 ✓** — agent surface: MCP server exposing `browser.*` tools. Local mode end-to-end.
- **Phase 2 ✓** — cross-host bridge: `holo mcp-remote` ferries stdio MCP through any user-supplied transport (ssh, `kubectl exec`, `aws ssm`, custom). Banner stripping + startup-timeout diagnostics. See `docs/cross-host.md`. The originally-planned registry-and-CA architecture was deferred — pragmatic transport-bridge meets the actual user need ("agent on B, daemon on A") without standing up new infrastructure. If a registry becomes necessary, it'd be Phase 2.5.
- **Phase 3 (opt-in)** — CDP adapter for tests that genuinely need structured network capture. Keep simple architecture as default.

The original roadmap had a "per-origin assertion plugins" phase between agent
surface and cross-host. We dropped it: a capable agent reads the page on
demand via `read_global` / `send_command` and figures out the structure
itself. Site plugins shipped by the framework would just freeze that derivation
and rot as sites redesigned. If a particular extraction proves expensive or
flaky enough to be worth caching, that's a recipe the agent (or its memory
layer) owns, not the daemon.

## Open questions

1. **Calibration maintenance.** First-click captures cursor + screenshot ring buffer; subsequent runs auto-click via OpenCV feature match (SIFT/ORB over raw template). Capture flow is settled; long-tail maintenance (browser updates, theme drift) needs operational answers.
2. **Concurrency in same-host mode.** Multi-tab and multi-host fall out of the registry abstraction in cross-host mode. Same-host concurrency may need explicit session-multiplexing in the local channel layer.
3. **Hermetic mode.** Auth-piggyback is the wedge, but some tests want a clean profile. Add an opt-in `clean-profile` flag that launches a separate browser instance, sacrificing the auth wedge for reproducibility.
