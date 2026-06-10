# Embedded holo in tai

## Goal

One binary on disk. One install step. When a user types `tai`, they get a shell with browser + screen automation builtins. When they type `bash` (or `sh`, `rbash`, `dash`, …) — even if it's the same binary on disk — they get plain bash with no extra surface area and no additional resource cost.

The embedded-holo runtime ships *inside* the tai binary, including the SikuliX screen bridge. No `install-screen` step. No second download. No "did you remember to install the bridge?" support burden.

## The dispatch rule (load-bearing)

The first decision the binary makes is based on `basename(argv[0])`. Three branches, three roles:

| `basename(argv[0])` | Role | Behavior |
|---|---|---|
| `tai` | User shell | Interactive shell or script runner with embedded holo. Browser + screen builtins registered in the shell command table. Lazy Python init on first builtin or `@` invocation. |
| `tcsh` | **Control shell** | Discards all positional arguments and flags. Eagerly initializes the embedded Python runtime, constructs the `HoloMCPServer`, and serves MCP-over-stdio until EOF. The sole purpose of this name is to let a remote agent control the browser and screen on this host. |
| Anything else (`bash`, `sh`, `rbash`, `dash`, …) | Plain bash | Embedded holo is **not** initialized, builtins are **not** registered, the embedded Python interpreter is **not** loaded. Indistinguishable from upstream bash. |

```c
int main(int argc, char **argv) {
    const char *name = basename(argv[0]);
    if (!strcmp(name, "tcsh")) {
        return control_shell_main();      // eager MCP server over stdio
    }
    int enable_holo = !strcmp(name, "tai");
    return tai_main(argc, argv, enable_holo);
}
```

### The three roles in plain words

- **`tai`** is what a human types at the terminal or what a script's `#!` line points to. It's a shell first — the embedded holo is *additive*, available to scripts and at the prompt via builtins, but the shell side of tai is the primary identity. Cold start when nothing holo-related is touched is unchanged from bash.

- **`tcsh`** is what an agent on a remote host invokes via SSH. It is not a shell at all in this mode — the name is reused purely because it's a familiar shell-like name that tells the user "this binary is in control-surface mode now." It speaks MCP over stdio, exposes the browser/screen tool surface, and exits when its stdin closes. There is no prompt, no script execution, no `@` operator — `tcsh somescript` and `tcsh -c whatever` both do the same thing as bare `tcsh`: serve MCP.

- **Any other name** is plain bash. Same binary on disk, none of the holo cost. `libpython` is linked in but the page never gets touched, so it stays paged out.

### Why eager init in control-shell mode

For `tai`, lazy init is right — a tai shell session may never invoke a holo builtin, so paying ~80 ms of `Py_InitializeEx(0)` upfront would punish the common case. For `tcsh`, the *entire purpose* of the invocation is to serve MCP; there's no other code path. Eager init means the first MCP `initialize` request gets answered without an additional cold-start delay, and any startup failure (missing native cache, broken Python state) surfaces immediately as a process exit rather than mid-session.

### Install layout

```
/usr/local/bin/tai             (the binary)
/usr/local/bin/tcsh -> tai     (control-shell name; required for A→B MCP)
/usr/local/bin/bash -> tai     (optional — turns this binary into the system bash)
/usr/local/bin/sh   -> tai     (optional)
```

Symlinks are created at install time (Homebrew formula, pkg postinstall, or `make install`). The three load-bearing names are `tai` (user shell), `tcsh` (control shell), and `bash` (plain bash). All hit the same file on disk; argv[0] is the entire dispatch signal.

This mirrors busybox's multi-call binary pattern and bash's own `sh`-vs-`bash` argv[0] detection.

### Why not subcommands

`tai mcp script.tai` is ambiguous (subcommand or first positional argument?), can't coexist with a script named `mcp`, and breaks every shell convention. argv[0] dispatch sidesteps all of this because it's set at exec time and doesn't propagate through child processes.

### Why not an env var

Env vars travel with `execve(2)`. A `TAI_MODE=holo`-style switch would inherit into every child process tai spawns — `osascript`, the SikuliX bridge subprocess, anything a shell pipe runs. Diagnostic clarity and `ps`/`top` output also suffer (everything shows up as `tai`). argv[0] is what every standard introspection mechanism reflects and it doesn't propagate.

## What's embedded

When invoked as `tai` or `tcsh`, the binary contains the same payload — the same vendored holo subset, the same bundled native deps. The only difference is *how it's reached*: `tai` exposes it as shell builtins and a lazy-init pathway; `tcsh` exposes it as MCP tools and an eager-init pathway.

### Surface in `tai` (user shell)

Holo capabilities appear as **shell builtins** in the command table:

| Group | Builtins |
|---|---|
| Browser (AppleScript) | `browser_navigate`, `browser_new_tab`, `browser_close_active_tab`, `browser_activate_tab`, `browser_list_tabs`, `browser_read_active_url`, `browser_read_active_title`, `browser_reload`, `browser_back`, `browser_forward`, `browser_execute_js` |
| Screen (bundled SikuliX bridge) | `screen_click`, `screen_move`, `screen_type`, `screen_key`, `screen_scroll`, `screen_shot`, `screen_find_image`, `screen_user_capture` |
| Templates | `ui_template_capture`, `ui_template_find`, `ui_template_click`, `ui_template_list`, `ui_template_delete` |
| Activation | `app_activate` |

Each builtin parses its arguments in C, calls into the embedded Python via CPython's C API, and writes the result to stdout / sets `$?`. The Python side is the vendored `holo` codebase under `vendor/holo/`.

### Surface in `tcsh` (control shell)

The same Python objects are wired up as **MCP tools** instead of shell builtins. The tool registrations are exactly the corresponding entries from the standalone `holo.mcp_server` module, restricted to the browser + screen + template + activate subset above. There is no shell prompt, no command table, no script parser — `tcsh`'s main loop is just `HoloMCPServer.serve_stdio()`.

The MCP tool names match the standalone holo CLI's tool names verbatim (`browser_navigate`, `screen_click`, etc.), so an A-side `.mcp.json` and any prompt-side training that expects the standard holo tool surface keeps working without modification.

### Python interpreter + module bundle (frozen, in-binary)

- CPython 3.13 (statically linked, `--without-shared --enable-optimizations`)
- Pure-Python stdlib (already mostly frozen in CPython 3.13+)
- Vendored `holo.*` subset (see below)
- Pure-Python deps: `pyperclip`, `pyautogui`, `certifi`

### Native libraries (bundled, extracted to cache on first use)

- pyobjc framework bindings: Quartz, Vision, AppKit (~100 MB)
- SikuliX screen bridge (~80 MB self-contained JVM + Jython + SikuliX)

Bundled as appended-tarball with offset table, the same approach AppImage and PyInstaller `--onefile` use. On first use, contents extract to:

```
~/Library/Caches/tai/native-<binary-sha256>/
```

The sha256 in the cache path means a tai upgrade silently provisions the new payload; no user action, no version-check cron, no "did you forget to reinstall?" footgun.

## What's NOT embedded

These exist in standalone holo but are explicitly excluded from tai's bundle:

| Excluded | Reason |
|---|---|
| Bookmarklet channel (`holo.channel`, `holo.ws_server`, `holo.registry`, `holo.framing`, popup HTML/JS) | Out of scope — AppleScript-only browser ops |
| mDNS announce (`holo.announce`) | Tai doesn't announce sessions |
| Discovery cache (`holo.discover`) | Tai doesn't browse for sessions |
| Capabilities probe / endpoint (`holo.capabilities`, `holo.capabilities_server`) | Tai doesn't expose host inventory |
| Dispatch broker (`holo.dispatch`) | `@` consults an **external** broker (see below) |
| MCP `--listen` TCP transport / `mcp-remote` / `connect` | The control shell speaks stdio MCP only; the TCP listener and A-side proxies stay in standalone holo |

The stdio MCP server itself (the FastMCP loop, the JSON-RPC framing, the tool registrations for browser/screen) **is** embedded — that's what `tcsh` runs. What's excluded is the *additional* MCP transport surface (`--listen`, `mcp-remote`, `connect`) that lives in standalone holo for cross-host bridging scenarios. A→B already gets bridged by A's `holo mcp-remote` + SSH; tcsh on B doesn't need its own bridging mode.

Pure-Python deps dropped: `websockets`, `starlette`, `uvicorn`, `zeroconf`, `async_timeout`. `mcp` (the FastMCP package) and its small transitive closure (`anyio`, `httpcore`, `httpx`, `pydantic`) **are** kept because `tcsh` needs them.

Net: the embedded Python bundle is ~20–25 MB compressed (the `mcp` package's transitive closure adds ~5 MB over the bookmarklet-less subset). The bulk of the binary's size comes from pyobjc (~100 MB) and the SikuliX bridge (~80 MB), both of which are unavoidable given the requirement to do screen automation without a separate install.

## The `@` operator (external broker)

`@ {selector} prompt /done` in tai scripts continues to POST to an external dispatch broker over the existing `/dev/tcp/host/port` path. This is the current `agent_dispatch.c` code, unchanged.

The broker is **not** embedded. It can be:
- A standalone holo daemon (`holo dispatch-broker`).
- A separate tai process running in broker mode (not in scope for this design).
- Any conforming implementation of `docs/dispatch-protocol.md`.

Broker location is controlled by `$TAI_DISPATCH_URL` (current default preserved).

Rationale: the broker is a meeting place for droids. It must persist across script lifetimes and accept long-lived WebSocket connections from agents on other machines. Embedding it in every tai invocation creates lifecycle confusion (which tai owns it?), wastes resources (every script spawns a broker), and doesn't help droid discoverability (droids would have to connect to a fresh port per script). The current external-broker model is correct.

## Use cases / wire shapes

### A→B control shell via SSH (the primary remote use case)

A's `.mcp.json`:

```json
{
  "mcpServers": {
    "holo": {
      "command": "/usr/local/bin/holo",
      "args": [
        "mcp-remote", "--",
        "ssh", "-l", "balexa100", "192.168.1.32",
        "/usr/local/bin/tcsh"
      ]
    }
  }
}
```

A's Claude:

1. Spawns A's local standalone `holo mcp-remote` (the existing stdio-proxy wrapper around an arbitrary transport).
2. `mcp-remote` execs `ssh -l balexa100 192.168.1.32 /usr/local/bin/tcsh`. The SSH command is **bare `tcsh`** — no script, no flags. The name is the entire signal.
3. sshd on B execs `/usr/local/bin/tcsh` with `argv[0] = "tcsh"`.
4. The binary takes the control-shell branch: eagerly inits Python, constructs `HoloMCPServer`, calls `serve_stdio()`. First cold start on B extracts the bundled native cache to `~/Library/Caches/tai/native-<sha256>/` (~500 ms one-time); subsequent runs skip extraction.
5. When the user on A says *"navigate to https://example.com using holo"*, A's Claude emits an MCP `tools/call` for `browser_navigate` over the SSH pipe; tcsh on B runs the corresponding Python tool; AppleScript fires on B's Chrome; the response goes back to A.

The user on A never installs holo separately on B, never runs `install-screen`, never thinks about a daemon. The single tai binary on B handles it all by virtue of being invoked under the `tcsh` name.

### SSH-launched tai script

```
$ ssh B /usr/local/bin/tai /path/to/script.tai
```

argv[0] = `tai`. The script runs on B, calls `browser_*` (Apple Events) and `screen_*` (bundled SikuliX) as **shell builtins**, prints output. stdout/stderr stream back to A over the SSH pipe.

This is distinct from the control-shell case in two ways:
- It runs a *script you authored*, not the MCP server. A's side reads script output, not JSON-RPC.
- The script can use the `@` operator, which goes to an external dispatch broker. The control shell doesn't have `@` — it's an MCP server, not a shell.

For the script to use `@`, B must have an external broker reachable at `$TAI_DISPATCH_URL` (typically `127.0.0.1:7081`).

### Local interactive tai

```
$ tai
tai$ browser_navigate https://example.com
tai$ screen_shot /tmp/shot.png
```

argv[0] = `tai`. Embedded holo is dormant until the first builtin call. Cold builtin call = `Py_InitializeEx(0)` (~80 ms) + AppleScript dispatch. Subsequent calls = direct Python invocation (<5 ms each).

### Local tai script

```
$ tai script.tai
```

Same as interactive. Pure shell scripts (no builtins, no `@`) never load Python; cold start stays under 10 ms.

### SSH-launched interactive tai on B

```
$ ssh -t B /usr/local/bin/tai
```

argv[0] = `tai`. A gets an interactive tai shell on B. Same builtins available. Subject to the usual SSH-TCC caveats on macOS: builtins requiring Screen Recording / Accessibility need the sshd process (or its ancestor) to hold those grants.

### Plain bash via the same binary

```
$ /usr/local/bin/bash script.sh       # /usr/local/bin/bash -> tai
```

argv[0] = `bash`. Embedded holo is not initialized. The binary behaves identically to upstream bash. `browser_navigate` is not a builtin — `command not found`.

### Discarded args under `tcsh`

```
$ tcsh somescript           # somescript is ignored — MCP server starts
$ tcsh -c 'whatever'        # -c arg ignored — MCP server starts
$ tcsh --help               # --help ignored — MCP server starts (and probably confuses an interactive user)
```

This is by design. The control shell has exactly one job, and accepting positional args would create ambiguity ("was the script supposed to run before the MCP server, after, or instead?"). Invoking `tcsh` always means *serve MCP on stdio*.

## Build / vendoring

```
tai/
├── …                          # bash sources
├── agent_dispatch.c           # existing HTTP-to-broker for @ operator
├── builtins/
│   └── holo_*.c               # one C file per holo builtin
├── embedded/
│   ├── frozen_holo.c          # generated: frozen Python module table
│   ├── native_payload.S       # generated: pyobjc + SikuliX appended-tarball
│   └── boot.c                 # Py_InitializeEx hook, cache extraction
└── vendor/
    └── holo/                  # git subtree of holo repo at a pinned tag
```

Build flow:

1. Compile tai C sources as usual.
2. `freeze` script reads `vendor/holo/src/holo/{daemon,channel-OMITTED,browser_chrome,bridge,templates,_macos,mcp_server-STRIPPED}.py` plus the resolved pure-Python deps, emits `frozen_holo.c` (a `_frozen_modules`-style table that CPython can import without filesystem access).
3. `bundle` script downloads / locates pyobjc + the prebuilt SikuliX bridge, creates the appended-tarball, embeds via linker directives.
4. Link: tai .o files + frozen_holo.o + libpython3.13.a + native_payload.o → `tai`.
5. Install: copy `tai` + create symlinks (`tcsh`, optionally `bash`/`sh`).

### Holo subtree sync

The vendored holo code lives under `vendor/holo/`. `git subtree` is used (not submodule) so a fresh clone has the sources present without an `--init` dance and so subtree commits land in tai's history as ordinary squash + merge commits.

**One-time setup on a fresh clone** (subtree needs to know where to fetch from; the URL isn't stored in the working tree):

```
$ git remote add holo git@github.com:nospaceleftondevice/holo.git
$ git fetch holo --tags
```

**Initial import** (already done at `vendor/holo` for `v0.1.0a32`, recorded as commit `4788d77`):

```
$ git subtree add --prefix=vendor/holo holo v0.1.0a32 --squash
```

**Bumping the pin** to a newer release tag:

```
$ git fetch holo --tags
$ git subtree pull --prefix=vendor/holo holo <new-tag> --squash
```

The squash commit's message records which holo commit the vendored tree was synced from, so `git log -- vendor/holo` is the authoritative record of "what version is pinned."

**Pushing tai-side changes back to holo** (rare — the contract is stable; do this only when a fix in vendored holo originates in tai's checkout):

```
$ git subtree push --prefix=vendor/holo holo <branch-on-holo-side>
```

**What to pin to.** Always a release tag (`v0.1.0a<N>`) — never `main`. Tags are immutable; main moves under us. A holo release that breaks tai (because we pinned to main and main drifted) is harder to diagnose than a deliberate pin bump.

The dispatch protocol spec (`docs/dispatch-protocol.md`) lives authoritatively in the **holo** repo and is referenced by tai's `agent_dispatch.c` comments. Tai vendors a read-only copy under `vendor/holo/docs/`.

## Sizes and startup cost

| Scenario | Cold start | Working-set cost |
|---|---|---|
| `bash` (argv[0] = bash) | <10 ms | libpython linked but never touched; stays paged out |
| `tai` shell, no holo builtins used | <10 ms | same — Python interpreter never initializes |
| `tai`, first holo builtin call | ~600 ms one-time (Py_Initialize + native extract on cold cache) | ~40 MB resident after init |
| `tai`, subsequent holo builtin calls | <5 ms | same |
| `tcsh` (control shell), first invocation on a host | ~600 ms one-time (Py_Initialize + native extract); subsequent runs ~100 ms (interpreter init only) | ~40 MB resident |
| `tcsh`, steady state per MCP tool call | <5 ms (browser_*) / ~20 ms (screen_* roundtrip to bridge) | same |
| Binary size on disk | n/a | ~180 MB |

The 180 MB binary is real. Breakdown:
- ~30 MB: bash + tai C code + statically linked libpython + frozen Python bundle
- ~100 MB: pyobjc native libs (Quartz, Vision, AppKit)
- ~80 MB: SikuliX bridge (JVM + Jython + SikuliX libs)
- (offset table + manifest overhead is negligible)

For comparison: VS Code's `code` binary is ~200 MB; Claude Code's `claude` is ~90 MB; `mongod` is ~150 MB. 180 MB for a shell+full-browser+screen-automation runtime is large but not anomalous.

A `tai-slim` variant (browser-only, no screen bridge, ~50 MB) is a possible future build target. Not in scope for v1.

## Initialization details

### `tai` (lazy)

On the first holo builtin call:

1. Builtin's C wrapper checks a static `interpreter_state` flag.
2. If unset: acquire a mutex, call `Py_InitializeEx(0)` (the 0 disables Python's signal handler installation — bash owns signals).
3. Import the frozen `holo.mcp_server` module and call its construction routine. This builds the `HoloMCPServer` Python object that owns the bridge subprocess handle.
4. Store the object in a static C pointer.
5. Release the mutex, mark initialized, proceed to the actual call.

Subsequent builtin calls skip steps 2–4 entirely; they just `PyObject_CallMethod` on the stored pointer with the parsed arguments.

### `tcsh` (eager)

`control_shell_main()` runs all of the above unconditionally and immediately, then calls `HoloMCPServer.serve_stdio()`. There's no shell command table, no signal-handler dance with bash — the binary is essentially a thin C launcher around the embedded Python's MCP server loop.

### Bridge subprocess

The SikuliX bridge subprocess is spawned by the Python `BridgeClient` on its own first need (the first `screen_*` call, not the first builtin/tool invocation). Browser-only sessions — whether `tai` or `tcsh` — never spawn the JVM.

### Cache extraction

On the first native call that needs an extracted library:

1. Check `~/Library/Caches/tai/native-<binary-sha256>/.stamp`. Present → already extracted, use it.
2. Absent → extract the appended-tarball to a temp dir, atomically rename to the final path, write `.stamp`. Subsequent processes find `.stamp` and skip extraction.
3. The sha256 in the path means a tai upgrade leaves the old cache intact (cleanup can be a future feature; ~180 MB per old version is the cost of keeping rollbacks fast).

### Cleanup on exit

For `tai`, the shell's normal exit path calls `shell_holo_teardown()` if the interpreter was initialized.
For `tcsh`, the same teardown runs when `serve_stdio()` returns (stdin EOF or fatal error).

Either way:

1. `BridgeClient.shutdown()` on the Python side, which SIGTERMs the SikuliX subprocess.
2. `Py_Finalize`.

If the process is killed (SIGKILL, panic), the SikuliX subprocess detects parent death via its existing watchdog and exits within ~1s. No orphaned JVMs.

## Out of scope (v1)

- **MCP `--listen` TCP transport.** Control shell speaks stdio MCP only. Cross-host bridging happens at A via `holo mcp-remote` + SSH; B's tcsh doesn't need to listen on a port.
- **Tai as a dispatch broker.** Brokers are external; tai is only a client of `/dispatch`.
- **Linux/Windows.** macOS only. pyobjc and AppleScript browser ops are Mac-specific. A future cross-platform variant would drop those subsets entirely and is a separate design.
- **Bookmarklet.** Dropped. Browser ops are AppleScript-only. Sites where Chrome's "Allow JavaScript from Apple Events" toggle is off lose `browser_execute_js`; that's the trade-off for a smaller binary and fewer moving parts.
- **Auto-install / auto-update.** The binary is the install. No background updates. Users updating tai get the new bridge through the bundled native payload.
- **mDNS announce / discovery / capabilities probe.** Tai's control shell is a tool surface, not a discoverable host. Standalone holo retains these for the cases that need them.
- **Mach-O codesigning / Gatekeeper notarization of the bundled artifact.** `make -C embedded bundle` appends a gzip tarball + 24-byte trailer *after* the Mach-O segments. Any `codesign` signature applied to the input bash is invalidated by the append; `spctl --assess` will reject the bundled file on SIP-enabled Macs that didn't either (a) build it locally, (b) install it via a package manager that ad-hoc resigns post-install, or (c) clear the quarantine attribute (`xattr -d com.apple.quarantine /path/to/tai`). The PR's distribution model is SSH-then-`scp`, not Mac App Store, so this is an explicit trade-off rather than a defect. A future redesign that wants App Store distribution would store the payload as a side-car `.dat` next to the binary (locate via argv[0]-relative path) so the executable can be signed and notarized normally.

## Open questions

1. **Cache eviction policy.** A user who upgrades tai monthly accumulates ~180 MB of stale native caches per version. A simple "keep latest 2 versions" sweep at startup is probably right. Cost: one `readdir` + `stat` per startup. Acceptable.
2. **SIGINT propagation under tai-with-Python-active.** Bash and Python both install SIGINT handlers. `Py_InitializeEx(0)` keeps bash's installed, but signal delivery during a Python call needs explicit handling (probably a C-side trampoline that calls `PyErr_SetInterrupt` so Python sees `KeyboardInterrupt`). Detail to nail down during implementation.
3. **Inter-process bridge sharing.** Multiple tai processes (e.g., two SSH sessions) each spawn their own SikuliX bridge subprocess today. Sharing one bridge across processes via a UNIX socket is possible but complex. Defer until measured to matter.
