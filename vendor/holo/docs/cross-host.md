# Cross-host setup

Phase 1 assumes the agent (Claude Code, Codex, …) runs on the same
host as the browser. If you want to run the agent on a different
machine and drive a remote browser, use `holo mcp-remote`.

## Use cases

There are two practical setups; only one needs `mcp-remote`.

**Same-host with remote terminal.** Daemon, browser, and agent CLI
all on Host A; you reach the agent CLI from Host B via `ssh hostA`
attaching to a tmux session. Already works in Phase 1 — no extra
config. Use this when you can.

**Cross-host.** Daemon and browser on Host A; agent CLI on Host B.
Use `holo mcp-remote` to bridge the agent's stdio MCP transport over
to Host A.

## Setup (cross-host)

On both hosts you need a `holo` binary on `PATH`. On Host A you also
need the bookmarklet installed in the browser you want to drive —
run `holo install-bookmarklet` on Host A and drag the 🔧 holo button
to the bookmarks bar.

There are two ways to wire it up. **On macOS Host A, use the
persistent-listener mode** — SSH-spawned processes don't inherit the
foreground app's Screen Recording permission, and macOS TCC won't
prompt non-interactive processes for it, so a fresh `holo mcp`
spawned by SSH every connection can never read window titles. The
listener mode keeps a single long-lived `holo mcp` running inside a
tmux session that *does* have Screen Recording permission.

### Persistent listener (recommended)

On Host A, in tmux:

```bash
holo mcp --listen 7777 --screen
```

That binds 127.0.0.1:7777 and waits for a single TCP client at a
time. Each connection must send a magic prefix (`HOLO/1\n`) before
any MCP traffic — drive-by browser fetches can't do that, since
browsers always send an HTTP request line first, so the listener
can't be reached from a malicious page on the same machine.

Calibrate locally — open a browser tab on Host A, click the
bookmarklet — the daemon registers the channel right there. Daemon
state lives across reconnects, so the agent on Host B doesn't have
to re-trigger calibration.

On Host B, in the project where you want to drive the remote browser:

```bash
cd ~/projects/foo
claude mcp add holo --scope project /path/to/holo mcp-remote -- \
    ssh -l balexa100 192.168.1.32 /usr/local/bin/holo connect localhost:7777
```

`holo connect` is a tiny stdio↔TCP bridge — it opens the TCP
connection on Host A's side of the SSH tunnel, sends the magic
prefix, and pipes bytes both ways. SSH provides the encrypted
transport; the prefix and the 127.0.0.1 bind are the access control.
No `nc` dependency.

### Spawn-per-connection (Linux / non-TCC remotes)

On Linux Host A (or anywhere TCC isn't in the picture), you can also
have `mcp-remote` spawn a fresh `holo mcp` per connection instead of
running a persistent listener:

```bash
claude mcp add holo --scope project /path/to/holo mcp-remote -- \
    ssh -A hostA holo mcp --screen
```

Anything after `--` is the verbatim command spawned. We're
transport-agnostic, so `kubectl exec`, `aws ssm start-session`, and
custom proxy scripts also work:

```bash
claude mcp add holo --scope project /path/to/holo mcp-remote -- \
    kubectl exec -i holo-pod -- holo mcp
```

```bash
claude mcp add holo --scope project /path/to/holo mcp-remote -- \
    aws ssm start-session --target i-abc123 \
    --document-name AWS-StartInteractiveCommand \
    --parameters command='holo mcp'
```

This mode is simpler — no listener to keep alive, no tmux, no
calibration-on-the-server-side. Use it when the remote OS doesn't
sandbox screen access by responsible-binary like macOS does.

## What `mcp-remote` does

It's a stdio MCP bridge: spawn the user-supplied command, pipe stdin
in, pipe stdout out, pipe stderr out. Three additions on top of a
plain `exec`:

1. **Banner stripping.** The agent's MCP client expects line-delimited
   JSON-RPC on stdin. SSH MOTDs, kubectl warnings, and similar prefixes
   would corrupt the protocol on the very first line. `mcp-remote`
   reads stdout line-by-line and skips anything that doesn't start
   with `{`, until the first JSON envelope arrives. After that it's
   plain passthrough.

2. **Startup timeout (default 15s).** If the transport halts on an
   interactive prompt (password / passphrase / 2FA) or the remote
   `holo` doesn't exist, the agent today sees a silent hang. We bound
   first-envelope arrival to `--startup-timeout SECS`, kill the child
   on timeout, and dump the captured banner + a structured error to
   our own stderr. Increase the timeout for slow connections:

   ```bash
   /path/to/holo mcp-remote --startup-timeout 30 -- ssh hostA holo mcp
   ```

3. **No tty.** We pipe stdin/stdout/stderr; we never allocate a pty.
   Interactive auth has no terminal to prompt against, so it fails
   immediately rather than hanging — you'll see a clear "no MCP
   envelope" error explaining what to fix.

## SSH requirements

Because `mcp-remote` doesn't allocate a tty, SSH needs to authenticate
non-interactively. Three ways to make that work:

- **Key-based auth + agent.** `ssh-add` your key into the SSH agent
  before launching the editor. `ssh -A` (agent forwarding) is in the
  examples for working through bastions.
- **Per-host config in `~/.ssh/config`.** Set `IdentityFile` and
  `User`/`HostName` for `hostA`. Don't set `BatchMode yes` globally —
  it'll break interactive ssh elsewhere.
- **ControlMaster.** A persistent connection makes subsequent MCP
  spawns fast and avoids re-auth. Add to `~/.ssh/config`:

  ```
  Host hostA
      ControlMaster auto
      ControlPath ~/.ssh/cm_%C
      ControlPersist 10m
  ```

  Or kick off a master ahead of time:

  ```
  ssh -fN -o ControlMaster=yes -o ControlPath=~/.ssh/cm_%C hostA
  ```

If SSH fails, the diagnostic block from `mcp-remote` shows the
captured stdout (typically including SSH's own error) and stderr —
that's where to look first.

## Sanity check

A quick smoke test, no Claude involved, just to confirm the bridge
mechanics:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | \
  /path/to/holo mcp-remote -- ssh hostA holo mcp
```

If you see a JSON response on stdout (and any banner on stderr),
the transport works and the only remaining piece is wiring it into
your agent's MCP config.
