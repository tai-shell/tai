# Dispatch Protocol — Wire Format

Concrete JSON-over-HTTP/WebSocket protocol for agent dispatch. Sits
between the tai shell, the holo daemon, and a browser-hosted agent
view (Droid). Companion to `docs/pool-dispatch-operator.md`, which
defines the language; this document defines what travels on the
wire.

## Actors

| Actor       | Where                  | Role                                  |
|-------------|------------------------|---------------------------------------|
| Shell (tai) | User's machine         | Issues `@ {sel} prompt /done`         |
| Holo        | User's machine         | Registry + dispatch broker            |
| Droid       | User's browser (xterm) | The actual injection point            |
| Agent       | Droid's xterm input    | The CLI receiving the keystrokes      |

The Droid is the *only* place that injects keystrokes. It uses the
same `send()` path that today's expect/send startup sequence uses,
which sits *above* the SSH transport — so the agent's multiplexer
choice (tmux, screen, none) does not affect the protocol.

If no Droid is currently attached to a matching agent, holo refuses
the dispatch with `no_droid_attached`. The shell's `else` action
(if any) runs.

## Connections

All endpoints are served by holo on `localhost:7082` (the existing
discover-helper port — the announce machinery and the dispatch
control plane share a single daemon).

| Endpoint              | Method | Who uses it      | Lifetime           |
|-----------------------|--------|------------------|--------------------|
| `/sessions`           | GET    | SPA + tools      | one-shot snapshot  |
| `/events`             | WS     | SPA + tools      | long-lived         |
| `/local-cloudcity`    | GET    | SPA              | one-shot           |
| `/control`            | WS     | Droid            | long-lived         |
| `/dispatch`           | POST   | Shell            | one-shot per `@`   |

`/sessions`, `/events`, and `/local-cloudcity` are unchanged from
the existing discover helper.

`/control` and `/dispatch` are new. The Droid attaches to `/control`
once on page-open and stays connected for the page's lifetime.
The shell hits `/dispatch` once per `@` invocation.

## Versioning

Every message carries `"v": 1`. Holo accepts only versions it
understands; missing `v` is treated as v1 for the alpha period and
becomes a hard error after v2 ships. Unknown fields are ignored
(forward-compatible additions). Renames or breaking changes bump
the version.

## `/control` — Droid registration

The Droid opens a WebSocket and sends one `register` message
identifying which agent it controls.

### `register` (Droid → holo)

```json
{
  "v": 1,
  "op": "register",
  "droid_id": "9c7c7e1d-…",         // Droid-generated UUID, persists for page life
  "agent_instance": "cloudcity-Right-Mac-19cf7a",
  "agent_capabilities": ["dispatch", "expect", "asciicast_slice"]
}
```

`agent_instance` is the unique label from the agent's holo
announce (the existing `instance` field — see
`docs/holo-cloudcity-tunnel-spec.md`). Holo refuses registrations
referencing unknown instances.

`agent_capabilities` is an opt-in list of features the Droid
supports. Holo must not send a dispatch for a capability that's
absent. v1 capabilities:

- `dispatch` — accept inject + sentinel-watch (required)
- `expect` — accept arbitrary expect-pattern waits (future)
- `asciicast_slice` — return captured output slice on completion

### `register_ack` (holo → Droid)

```json
{
  "v": 1,
  "op": "register_ack",
  "ok": true,
  "agent_instance": "cloudcity-Right-Mac-19cf7a"
}
```

On error (unknown instance, capability conflict):

```json
{
  "v": 1,
  "op": "register_ack",
  "ok": false,
  "error": "unknown_instance"
}
```

The Droid stays connected through registration failures so the
user can see diagnostic messages in the SPA console; the connection
is otherwise idle until a successful `register`.

### `ping` / `pong` (both directions)

```json
{ "v": 1, "op": "ping" }
{ "v": 1, "op": "pong" }
```

Sent every 30 s by either side. Connection is dropped if no
`pong` arrives within 60 s.

### Connection close

Either party MAY close the WebSocket cleanly. Holo treats Droid
disconnects as deregistration: any in-flight dispatches targeting
that Droid fail with `droid_dropped`. Unfinished dispatches that
have not yet been routed are unaffected.

## `/dispatch` — Shell submits a dispatch

```
POST /dispatch
Content-Type: application/json
```

### Request (Shell → holo)

```json
{
  "v": 1,
  "selector": "{coding:5s}",       // verbatim, with brackets
  "broadcast": false,              // true if outer brackets were [...]
  "prompt": "lint $file",          // already shell-expanded
  "sentinel": "done",              // identifier without leading slash; null if absent
  "block_id": null,                // UUID for do…end stickiness, or null
  "timeout_ms": 5000,              // from selector wait_config; null = pool default
  "capture": false                 // request asciicast slice in response
}
```

`selector` is sent verbatim with its outer `{}` or `[]`. Holo's
selector parser (lives in holo, not in the shell) interprets the
predicate grammar from the operator spec.

`broadcast` is a redundant convenience: holo can also tell from
`selector[0]`. Sent explicitly to make the routing decision
unambiguous on both sides.

`prompt` is the post-bash-expansion text. The shell does NOT
re-quote — holo treats the string as the verbatim message body.
Any prompt-rewriting (sentinel append, block prelude) happens
inside holo.

`block_id` is generated by the shell when a `do…end` block opens
and reused for every `@` inside the block. Holo pins one agent
to a `block_id` for the block's lifetime. `null` means "no
stickiness — match per dispatch."

`timeout_ms` is the wait-for-completion budget the runtime
applies to the sentinel watch. `null` means "use the holo pool
default for the matched agent."

`capture` opt-in: when true, holo returns the asciicast slice
between dispatch start and sentinel match in `output`. When
false, the response carries only metadata.

### Response — single-agent (`broadcast: false`)

```json
{
  "v": 1,
  "ok": true,
  "agent_instance": "cloudcity-Right-Mac-19cf7a",
  "nonce": "7f3a9c1b",
  "elapsed_ms": 1234,
  "output": "summary text..."      // only when capture: true
}
```

On failure:

```json
{
  "v": 1,
  "ok": false,
  "error": "timeout",
  "elapsed_ms": 5000,
  "agent_instance": "cloudcity-Right-Mac-19cf7a"   // when known
}
```

### Response — broadcast (`broadcast: true`)

```json
{
  "v": 1,
  "ok": true,
  "results": [
    {
      "agent_instance": "claude-Right-Mac-…",
      "ok": true,
      "nonce": "1a2b3c4d",
      "elapsed_ms": 980,
      "output": "..."
    },
    {
      "agent_instance": "gpt-Right-Mac-…",
      "ok": false,
      "error": "timeout",
      "elapsed_ms": 5000
    }
  ],
  "elapsed_ms": 5000
}
```

Top-level `ok` is true if at least one agent completed
successfully. The shell's exit code policy: `0` if `ok`, non-zero
otherwise. The full per-agent breakdown is available in the
response body (and via output capture when supported).

## Holo → Droid forwarding

Once holo has matched a `/dispatch` request to a Droid, it forwards
the dispatch over that Droid's `/control` WebSocket and waits for
a result.

### `dispatch` (holo → Droid)

```json
{
  "v": 1,
  "op": "dispatch",
  "dispatch_id": "0f3a-…",          // holo-generated, unique per dispatch
  "payload": "lint /tmp/foo.c\nWhen complete, print exactly:\n/done:7f3a9c1b\non its own line.\n",
  "expect": "/done:7f3a9c1b",       // exact line to wait for; null if no sentinel
  "timeout_ms": 5000,
  "capture": false
}
```

`payload` is the full text the Droid types into the agent's
terminal — already includes the prompt rewriting (sentinel-append
prelude). The Droid does not interpret it; it calls the existing
`send()` path (xterm.input / xterm.paste).

`expect` is the exact line the Droid watches for in its
`shadowBuffer`. A null `expect` means "fire and forget" — the
Droid responds immediately with `ok: true`.

### `dispatch_result` (Droid → holo)

```json
{
  "v": 1,
  "op": "dispatch_result",
  "dispatch_id": "0f3a-…",
  "ok": true,
  "elapsed_ms": 1234,
  "match_offset": 4823,             // shadowBuffer index of sentinel match
  "output": "..."                   // only when capture: true was requested
}
```

On timeout:

```json
{
  "v": 1,
  "op": "dispatch_result",
  "dispatch_id": "0f3a-…",
  "ok": false,
  "error": "timeout",
  "elapsed_ms": 5000
}
```

## Error codes

| Code                 | Meaning                                         | Where it can originate |
|----------------------|-------------------------------------------------|------------------------|
| `no_match`           | Selector matches no agent in the registry       | holo                   |
| `static_miss`        | Selector predicate cannot be satisfied (e.g. a model that doesn't exist) | holo |
| `no_droid_attached`  | Agent matched, but no Droid is registered for it | holo                   |
| `saturation_timeout` | Matching agents exist but all busy through wait | holo                   |
| `selector_invalid`   | Bad selector syntax                             | holo                   |
| `timeout`            | Sentinel did not arrive within `timeout_ms`     | Droid → holo           |
| `droid_dropped`      | Droid disconnected mid-dispatch                 | holo                   |
| `unknown_instance`   | Droid registered for an instance holo has not seen | holo                |
| `internal`           | Daemon-side bug                                 | any                    |

The shell maps any non-`ok` response to a non-zero exit code and,
if the dispatch syntax included an `else` clause, runs the else
action. The error code is exposed in `$TAI_LAST_ERROR` for scripts
that want to branch on it.

## Block stickiness — `block_id` mechanics

When the shell parses `@ {sel} do …lines… end`, it generates a
fresh UUID and uses it as `block_id` for every dispatch in the
block. Holo's bookkeeping:

1. **First** dispatch with a given `block_id` runs ordinary
   selector matching, picks an agent, and stores
   `block_id → agent_instance → droid` in a per-block table.
2. **Subsequent** dispatches with the same `block_id` skip
   selector matching and route directly to the pinned Droid.
3. **End of block** is marked by an explicit `release` request
   (see below). Holo evicts the entry. Future dispatches with the
   same `block_id` are treated as new (re-pick).

Crash recovery: holo expires `block_id` entries 5 minutes after
their last activity, so a shell that dies mid-block doesn't pin
agents indefinitely.

If the pinned Droid disconnects mid-block, the next dispatch in
the block fails fast with `droid_dropped` — there is no implicit
re-pick (matches the spec's "block fails immediately" rule).

### `release` (Shell → holo)

```
POST /dispatch/release
{ "v": 1, "block_id": "…" }
```

Sent when the shell finishes a `do…end` block (or aborts it).
Idempotent. Errors are best-effort; the shell does not block on a
failed release.

## Output capture (`capture: true`)

The Droid's existing `AsciicastRecorder` keeps a rolling 10-min
buffer of the agent's PTY output. When `capture: true`, the Droid:

1. Records the `(rec_t0, line_count_0)` cursor at dispatch start.
2. After `expect` matches, slices from `(rec_t0, line_count_0)`
   to the line containing the match.
3. Strips ANSI escapes (best-effort — sufficient for grep, may
   not be sufficient for tools that depend on cursor positioning).
4. Returns the resulting plain text in `output`.

Without `capture`, no slice is ever computed — the dispatch is
fire-and-watch with only the metadata returned. This is the
default because slicing has nontrivial cost for chatty agents.

## Lifecycle summary

```
   page open                       agent observed
        │                                │
        ▼                                ▼
  Droid /control WS  ◄── register_ack ── Holo
        │
        │  (idle, ping/pong)
        │
        │           shell:
        │           @ {coding} prompt /done
        │                      │
        │                      ▼
        │           POST /dispatch  ──►  Holo
        │                                  │
        │                                  ▼ pick agent, find droid
        │           ◄────── op:dispatch ───┤
   send()/paste()                          │
   shadowBuffer watch                      │
        │                                  │
        ├── op:dispatch_result ──────────► │
        │                                  ▼
        │                       ◄── 200 OK { ok, … }
        │
        │  (back to idle)
```

## Implementation notes (informative)

- The shell speaks plain HTTP for `/dispatch` to keep the C-side
  builtin simple. A Unix-domain socket variant for the same
  endpoint may follow if perf becomes interesting.
- The Droid's `send()` already exists in
  `examples/desktop/c2w-init.js`. Wiring `/control` into it is a
  small JS change: open the WS, register, on `dispatch` call
  `send(payload)` and watch `shadowBuffer` for `expect`.
- Holo's selector parser is the only piece of the operator
  language that lives in holo (everything else is in the shell).
  Keep them in sync: any selector grammar change means a holo
  release and a shell release together.
- Authentication: v1 trusts `localhost`. Future remote-controller
  modes will require token auth on `/dispatch` and origin checks
  on `/control`.

## Decisions locked

1. Droid is the universal injection point. No `tmux send-keys` /
   `screen -X stuff` path in v1.
2. `localhost:7082` carries everything (announce + control +
   dispatch). One process, one port.
3. Selector parsing is holo-side. The shell sends the bracketed
   verbatim string.
4. Block stickiness via shell-generated UUID. Holo trusts.
5. No-Droid-attached is a hard refusal, not auto-open. Auto-open
   is reserved for a future `auto_open` request flag.

## Open / deferred

- Streaming partial output during long dispatches (today the
  Droid only responds at sentinel match or timeout).
- Multiple sentinels per dispatch (`/progress`, `/error`).
- Cancel — sending an interrupt to a running dispatch.
- Cross-tab single-agent disambiguation when two browser tabs
  both register for the same instance.
- Rate-limit / backpressure when a script floods `/dispatch`.
- Authentication for non-localhost holo daemons.
