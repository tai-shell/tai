# tai's `on` keyword

`on` is tai's keyword for dispatching a shell body to every holo daemon matching a selector. It's the bash-level entry point to the cross-host fan-out feature designed in [holo/docs/resources.md](https://github.com/nospaceleftondevice/holo/blob/main/docs/resources.md).

```bash
on [holo:tag=video-files:5m] find . -type f -name "*.mp4" | wc -l /done
```

This runs `find … | wc -l` on every holo daemon announcing a resource tagged `video-files`, with each daemon's stdout flowing to the script's stdout in completion order.

## Why `on`, not `@`

`@` is tai's existing AI agent dispatch operator (`@ {agent} prompt /reply`). It's wired to a CloudCity-style HTTP transport and the dispatched payload is an AI prompt, not a shell command.

`on` is a parallel pipeline targeting holo daemons over MCP. It reuses `@`'s grammar (selector / body / sentinel / else / do…end) but routes to a Python dispatcher (`tai_runtime.holo_on.dispatch`) instead of the AI agent transport.

The two coexist — neither operator interferes with the other.

## Grammar

```
on SELECTOR BODY /SENTINEL [else COMMAND]
```

or block form:

```
on SELECTOR do
  BODY-LINE-1
  BODY-LINE-2
  ...
end [else COMMAND]
```

### Selector

```
[holo:PREDS:SUFFIXES]    # broadcast — fan out to every match
{holo:PREDS:SUFFIXES}    # single target — must match exactly one
```

**Predicates** (comma-joined within one segment, AND-semantics):

| | Meaning |
|---|---|
| `tag=X` | Match daemons announcing any resource tagged X |
| `name=X` | Match daemons announcing a resource named X |
| `host=X` | Match the daemon whose host == X |
| `resource=X` | Synonym for `name=X` at the discovery layer |

**Suffixes** (colon-joined, any order, all optional):

| | Meaning |
|---|---|
| `5m` / `30s` / `1h` | Per-dispatch timeout |
| `settle=500ms` / `settle=2s` | mDNS discovery settle window (default 1s) |
| `tagged` | Output mode — every line prefixed `host:resource:` |
| `json` | Output mode — each frame emitted as JSONL `{host, resource, fd, data}` |

### Body

Captured **verbatim**, character by character, until newline or `;`. Shell expansion does NOT happen at capture time — `$VAR`, `$(...)`, `*.mp4`, etc. are passed through to the daemon to interpret.

This means **multi-line bodies require the `do … end` block form**. Heredocs and trailing-backslash line continuations don't work in the single-line form; the parser terminates the body at the first newline.

### Sentinel

A trailing `/identifier` (e.g. `/done`) is captured but not yet acted on at the daemon — placeholder for future synchronization semantics.

### `else` clause

Fires **once per failed host** with `$HOLO_HOST` rebound to that host's name. A "failure" is exec timeout, allowlist rejection, body exit ≠ 0, or unreachable daemon.

## Per-call env

Each dispatched body sees these in its env (set by the holo daemon):

| | |
|---|---|
| `$HOLO_HOST` | The daemon's hostname |
| `$HOLO_RESOURCE` | The matched resource name |
| `$HOLO_RESOURCE_PATH` | The absolute path the daemon pinned cwd to |

## Post-dispatch state

After an `on` invocation returns, the calling shell has:

- `$?` set to `max(per-host exit codes)`, or to the max(else exits) if any host failed and an else was provided.
- `$ON_HOSTS` as a bash array of `host:exit` strings — one per matched (host, resource) tuple, in target order. Re-bound on every `on` invocation (same lifecycle as `$PIPESTATUS`).

## Concurrency

Phase 3.D fans out to matched hosts **concurrently** via `asyncio.gather` in the dispatcher. Each host's frames emit in completion order, not target order. Each individual line is line-atomic per host (single `print()` under the GIL), so concurrent emits never split mid-line.

## Setup the daemons need

Every daemon participating in `on` dispatch must be running:

```bash
holo mcp --announce --resources-config ~/.config/holo/resources.toml
```

with a TOML config declaring at least one resource:

```toml
[resources.movies]
path = "/Volumes/movies"
tags = ["video-files"]
caps = ["exec:find", "exec:wc"]
```

The `caps` list is the **allowlist** of binaries the daemon will let dispatched bodies invoke. Shell builtins (`echo`, `cd`, etc.) are always available; everything else needs an explicit `exec:NAME` cap. See [holo/docs/resources.md §Q2](https://github.com/nospaceleftondevice/holo/blob/main/docs/resources.md) for the full enforcement design.

## Local-spawn transport

Phase 3.A through 3.D use a **local-spawn** transport for v1: every dispatch target spawns a fresh `holo mcp` subprocess and speaks MCP over its stdin/stdout. For development and same-host smoke this works without auto-tunnel setup. Production cross-host transport via the existing `holo tunnel up` flow lands in a future phase.

Tell the dispatcher how to spawn the local daemon via env vars (or override at the C bridge if you embed it):

| | |
|---|---|
| `HOLO_CLI` | Path to the holo binary (default: `holo` on PATH) |
| `HOLO_ON_EXTRA_ARGS` | Space-separated flags passed to the spawned daemon (typically `--resources-config /path/to/resources.toml`) |

## Examples

Three runnable examples ship in [`examples/holo-on/`](../examples/holo-on/):

- `count-mp4s.tai` — broadcast + default mode + `$ON_HOSTS` introspection
- `list-files-json.tai` — `:json` mode for JSONL output piping
- `per-host-stats.tai` — `do…end` block + `:tagged` mode + `else` per-failed-host + summary loop

## What's not yet supported

Tracked as follow-ups:

- **Multi-line bodies** outside the `do…end` form. A heredoc-style or backslash-continuation syntax is the natural extension; until then, multi-step bodies live in `do…end` blocks (one command per line).
- **Auto-tunnel transport** — currently spawns local daemons; production cross-host dispatch via the `holo tunnel up` flow needs a small additional bridge.
- **Per-frame streaming concurrency** — emits per-host as a block once that host's MCP call completes. Frame-level interleave would need streaming MCP support upstream.
- **Pre-deployed helper scripts** — large bodies (e.g. mp4 mdat-md5 fingerprinting) realistically live as standalone scripts pre-installed on each daemon, invoked by name from `on`. A "deploy helper" primitive would close the loop.

## See also

- [holo/docs/resources.md](https://github.com/nospaceleftondevice/holo/blob/main/docs/resources.md) — the cross-system design doc covering announce, exec, ACL, and the full Phase 0 decision record.
- [docs/pool-dispatch-operator.md](pool-dispatch-operator.md) — the `@` operator design for comparison.
