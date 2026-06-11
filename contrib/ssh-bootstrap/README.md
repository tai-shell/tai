# tai SSH bootstrap — agentless driver from Host A to Host B

A 4-file toolkit for driving a `tai` bundle on a remote Mac (Host B)
from a Claude session running on a local Mac (Host A) without
permanently installing anything on B. The binary lives in `/tmp`,
the listener is launched inside Terminal.app on B (TCC-aware), and
Claude on A talks to it over an SSH-tunneled TCP transport.

| File | Where it runs | What it does |
|---|---|---|
| `bootstrap.sh` | Host A | One-shot: scp the tai binary + a generated `launcher.command` to `/tmp` on Host B, then `ssh open` the launcher so it runs inside Terminal.app. |
| Generated `launcher.command` | Host B (inside Terminal.app) | Symlinks `/tmp/tai` → `/tmp/tcsh`, writes the per-session token to `/tmp/tai-token`, spawns `tcsh --listen` detached, auto-closes the Terminal window. |
| `mcp-bridge.py` | Host A | The MCP server command Claude spawns. Opens an SSH `-L` tunnel to Host B, sends the handshake, relays Claude's stdio to/from the tunneled socket. |
| `smoke.sh` | Host A | Manual end-to-end verification (initialize, tools/list, doctor, screen_move). Run after `bootstrap.sh` to confirm the round-trip works before plugging into Claude. |

## Why this layout

The naive `claude → ssh hostB tcsh` pattern fails on screen tools:
sshd is the process that exec'd `tcsh`, so macOS attributes
Accessibility / Screen Recording to **sshd**, which you cannot
grant. The SikuliX bridge's `Robot.mouseMove()` silently no-ops.

This layout sidesteps that by getting tcsh born inside Terminal.app:

```
Host A                                Host B
                                          
bootstrap.sh                          
   │                                  
   ├─ scp ──────────────────► /tmp/tai
   ├─ scp ──────────────────► /tmp/launcher.command
   └─ ssh open ──► sshd ──► open ──► LaunchServices ──► Terminal.app
                                                       │
                                                       └─ bash (launcher.command)
                                                            │
                                                            └─ tcsh --listen 7081
                                                               (TCC root: Terminal.app ✓)

Later, Claude session:

Host A                                Host B
                                          
claude ──► mcp-bridge.py              
                │                     
                ├─ ssh: read /tmp/tai-token
                ├─ ssh -L 7081:127.0.0.1:7081
                │                     
                └─ connect 127.0.0.1:7081 ◄─ ssh tunnel ─► tcsh --listen 7081
                   handshake (TAI/1 + token)                 (still under
                   stdio relay                               Terminal.app TCC)
```

`tcsh` is single-connection per launch (matches holo `mcp --listen`).
Each Claude session re-fires `bootstrap.sh` to get a fresh listener
+ fresh token. Re-running `bootstrap.sh` is idempotent: the launcher
kills any prior tcsh listener before starting a new one.

## Prereqs

- **Host A**: `tai-bundled-macos-universal2` downloaded somewhere
  (v0.1.0a38 or later — needs `tcsh --listen`). By default
  `bootstrap.sh` looks at `/usr/local/bin/tai`; override with
  `TAI_BINARY=…`. Python 3 (system default is fine; mcp-bridge.py
  uses stdlib only).
- **Host B**: macOS with Java (`brew install openjdk` is fine);
  Terminal.app granted Accessibility + Screen Recording in System
  Settings → Privacy & Security; SSH key-based auth from A to B
  configured (no password prompts).
- **SSH config**: works with whatever you'd normally use; the
  toolkit calls `ssh` and `scp` with no special options beyond
  `BatchMode=yes` on the bridge side (so prompts don't hang
  Claude). If your remote needs a specific identity / proxy /
  jump host, put it in `~/.ssh/config` on Host A.

## Quick start

```sh
cd contrib/ssh-bootstrap
chmod +x bootstrap.sh mcp-bridge.py smoke.sh

# 1. Bootstrap (ships binary + launcher, opens it inside Terminal.app)
TAI_BINARY=~/Downloads/tai-bundled-macos-universal2 \
    ./bootstrap.sh user@hostB

# A Terminal window briefly flashes on B's screen and auto-closes;
# the detached listener now lives at 127.0.0.1:7081 on B.

# 2. Verify
./smoke.sh user@hostB
# Expected: ✓ PASS, 26 tools, screen_move 500 500 succeeds — the
# cursor on B should also briefly jump to (500, 500).

# 3. Wire into Claude. Add to ~/.claude.json (or per-project):
#    {
#      "mcpServers": {
#        "tai-on-hostB": {
#          "command": "/full/path/to/mcp-bridge.py",
#          "args": ["user@hostB", "7081"]
#        }
#      }
#    }
```

Claude can now call `screen_move`, `screen_click`, `screen_type`,
`browser_*`, `ui_template_*`, etc. on the remote Mac.

## Environment knobs

`bootstrap.sh`:

| Var | Default | Notes |
|---|---|---|
| `TAI_BINARY` | `/usr/local/bin/tai` | Path to your downloaded tai-bundled binary on A. |
| `TAI_LISTEN_PORT` | `7081` | Port tcsh binds on B + tunnel local port. |
| `TAI_REMOTE_TOKEN_PATH` | `/tmp/tai-token` | Where the launcher writes the token on B. |
| `TAI_LOCAL_TOKEN_PATH` | `/tmp/tai-bridge-token` | Local copy on A. mcp-bridge.py doesn't use this (it re-fetches from B) — it's kept for operator inspection. |

`mcp-bridge.py` reads no env vars; everything's positional argv so
Claude's spawn config controls all the knobs.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `bootstrap: scp: permission denied` | SSH key auth not set up. `ssh-copy-id user@hostB`. |
| `bootstrap: open` runs but no Terminal window appears | macOS GUI session locked on B. The launcher needs a logged-in user. |
| `smoke.sh: handshake rejected: ERR bad handshake` | Token mismatch — either bootstrap didn't finish, or the listener is from a previous bootstrap with a different token. Re-run `bootstrap.sh`. |
| `smoke.sh: tools/list shows >0 tools but screen_move "no response"` | TCC failure on B. Open Terminal on B manually, run `tail -f /tmp/tai-listener.log` and re-run smoke; look for `Robot` / `Accessibility` errors. Most common fix: System Settings → Privacy & Security → Accessibility → ✓ Terminal. |
| Claude shows `mcp-bridge: ssh tunnel exited early` | Could not bind port 7081 on A (port already in use), or SSH host-key prompt blocking. Try `TAI_LISTEN_PORT=7082 ./bootstrap.sh ...`. |
| Cursor moves to wrong spot on Retina screen | macOS reports points, but SikuliX `Robot` operates in pixels. Halve your coordinates as a workaround. (Resolution-independent fix is a separate holo PR.) |

## Notes on the design choices

- **Single connection per launch.** Mirrors `holo mcp --listen`. The
  Claude session, the JVM bridge state, and the TCC chain are all
  per-tcsh-process. Multi-connection means concurrent JVM bridges
  competing for the SikuliX state, which is messy. Re-fire
  `bootstrap.sh` to get a fresh listener; cheap (~2 s).
- **Token over SSH, not env / CLI.** Tokens passed via env vars
  leak to `ps` and to children. A read-from-/tmp-via-ssh model
  keeps it scoped to whoever has SSH access — which is the same
  set of people who can already drive the remote anyway.
- **No tmux dependency.** `nohup` + `disown` + `setsid`-equivalent
  is enough on macOS; tmux would add an install requirement.
  Trade-off: you can't `tmux attach` to the listener's terminal,
  but the log goes to `/tmp/tai-listener.log` instead.
- **Bridge in Python, not bash.** The TAI/1 handshake needs read-
  exactly-one-line-from-socket-then-relay semantics. Pure-shell
  versions of that are fragile (`/dev/tcp` requires bash 4 which
  macOS doesn't ship; `nc` flag portability is bad). Python is on
  every Mac.
