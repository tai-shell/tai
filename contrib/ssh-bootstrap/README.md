# tai SSH bootstrap — agentless driver from Host A to Host B

A 4-file toolkit for driving a `tai` bundle on a remote Mac (Host B)
from a Claude session running on a local Mac (Host A) without
permanently installing anything on B. The binary lives in `/tmp`,
the listener is launched inside Terminal.app on B (TCC-aware), and
Claude on A talks to it over an SSH-tunneled TCP transport.

**The user wires `mcp-bridge.py` into Claude's MCP config and that's
it** — every Claude session triggers `mcp-bridge.py`, which itself
ships the binary (only when its sha differs from the remote copy),
generates a fresh per-session token + launcher, fires it inside
Terminal.app, waits for the listener, and bridges Claude's stdio.
No external `bootstrap.sh` invocation needed.

| File | Where it runs | What it does |
|---|---|---|
| `mcp-bridge.py` | Host A (spawned by Claude) | One-stop: scp binary if sha mismatches, generate + scp launcher.command, `ssh open` it inside Terminal.app, wait for tcsh, open SSH tunnel, handshake, relay stdio. |
| Generated `launcher.command` | Host B (inside Terminal.app) | Symlinks `/tmp/tai` → `/tmp/tcsh`, writes the per-session token to `/tmp/tai-token`, spawns `tcsh --listen` detached, auto-closes the Terminal window. Generated fresh by `mcp-bridge.py` per session. |
| `bootstrap.sh` | Host A (optional / debug) | Same setup steps as `mcp-bridge.py` does internally, but as a separate script. Useful for pre-warming a Mac, debugging the chain step by step, or running smoke.sh without Claude. Not required for normal use. |
| `smoke.sh` | Host A | End-to-end verification (initialize, tools/list, doctor, screen_move). Drives `mcp-bridge.py` with canned JSON-RPC; pass/fail in 30 s. |

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

`tcsh --listen` runs a **serial accept loop**: it serves one session at
a time (sessions drive singleton resources — the SikuliX JVM, the mouse,
the keyboard), but the listening socket outlives each session, so a
client whose connection drops can simply reconnect. It forks per
session, so each one gets a pristine interpreter and the SikuliX JVM is
reaped when that session ends. A failed handshake drops that connection
and keeps listening rather than killing the listener.

This matters because the connection runs over an `ssh -L` tunnel: a
tunnel hiccup used to be terminal, leaving a wedged `tcsh` on Host B
that had to be killed by hand before the next session could start.

Each Claude session still re-fires `bootstrap.sh` for a fresh listener
+ fresh token. Re-running `bootstrap.sh` is idempotent: the launcher
kills any prior tcsh listener before starting a new one (it holds the
port, and its old token wouldn't match anyway).

Because the listener persists, it also relies on the far side noticing
a dead client. Accepted sockets get TCP keepalive armed (~110s to reap
a peer that dies without a FIN), but through an `ssh -L` tunnel the
socket's peer is Host B's own sshd — so set `ClientAliveInterval` in
Host B's `sshd_config` if you want sshd to tear down forwarded channels
promptly when the client vanishes.

### SSH multiplexing (ControlMaster)

`ControlMaster` / `ControlPersist` in your `~/.ssh/config` for Host B is
a **good idea** — bootstrap makes several short ssh/scp calls, and
multiplexing collapses them onto one authentication.

But the bridge's `-L` port-forward deliberately opts **out** of it
(`ControlPath=none`, `ControlMaster=no` in `open_tunnel`), and that
override must stay. If the forward is multiplexed it becomes the
property of the shared master instead of the bridge process, and the
master outlives the session by `ControlPersist`. Consequences, all
observed:

* The master keeps holding local `PORT` after the bridge exits.
  Terminating the bridge kills its own client, not the master's
  listener, so the *next* session can't bind and you're locked out
  until `ControlPersist` expires.
* `ExitOnForwardFailure` stops being trustworthy — a multiplexed client
  that can't set up the forward has been seen exiting **0 with empty
  stderr**, which looks like success and then fails at connect time.
* A new session can silently inherit the master's **stale** forward,
  pointing at a listener that no longer exists. The tunnel looks
  healthy; the handshake fails for no visible reason.

If you ever see `ssh tunnel exited early`, check what holds the port:

```sh
lsof -nP -iTCP:7081
ssh -O check <host>      # is there a control master?
ssh -O exit  <host>      # clear it
```

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

# Wire into Claude. Add to ~/.claude.json (or per-project):
#    {
#      "mcpServers": {
#        "tai-on-hostB": {
#          "command": "/full/path/to/contrib/ssh-bootstrap/mcp-bridge.py",
#          "args": ["user@hostB", "7081"],
#          "env": {
#            "TAI_BINARY": "/Users/me/Downloads/tai-bundled-macos-universal2"
#          }
#        }
#      }
#    }
```

That's it. The first Claude session ships the binary (~3-30 s
depending on link speed), fires the listener, and starts serving
MCP. Subsequent sessions skip the binary scp (sha match) and start
in ~2-5 s.

Optional smoke test (before plugging into Claude):

```sh
TAI_BINARY=~/Downloads/tai-bundled-macos-universal2 \
    ./smoke.sh user@hostB

# Expected: ✓ PASS, 26 tools, screen_move 500 500 succeeds — the
# cursor on B should also briefly jump to (500, 500).
```

Optional manual bootstrap (debugging / pre-warming):

```sh
TAI_BINARY=~/Downloads/tai-bundled-macos-universal2 \
    ./bootstrap.sh user@hostB
```

Runs the same setup steps that `mcp-bridge.py` does internally —
useful when you want to verify each step manually.

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

`mcp-bridge.py` (host + port are positional argv; these env vars are
optional and typically set in Claude's spawn config):

| Var | Default | Notes |
|---|---|---|
| `TAI_BINARY` | `/usr/local/bin/tai` | Path **or** `http(s)` URL to the tai-bundled binary on A. A URL is downloaded once and cached under `~/Library/Caches/tai/downloads/`. |
| `MCP_BRIDGE_KEEP_BINARY` | unset | `=1` preserves only `/tmp/tai` on B across sessions (token, launcher, log, symlink, and cache are still wiped) so the next session's sha-check skips the ~191 MB re-ship. Trades a resident binary on B for fast reconnects. |
| `MCP_BRIDGE_KEEP_TRACES` | unset | `=1` skips cleanup entirely (leaves logs etc. on B for debugging). |

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
