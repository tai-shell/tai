# Host Dispatch — Wire Format

Extension to `dispatch-protocol.md` covering *host actions* — dispatches
addressed to a machine rather than an agent. The motivating use case is
spawning specialized browser sessions ("expert Nano instances") on
machines selected by inventory predicates, but the mechanism is
general-purpose: any privileged action a host can perform on its own
machine.

Companion to:

- `dispatch-protocol.md` — base wire format (actors, connections,
  versioning). All of that applies here unchanged.
- `pool-dispatch-operator.md` — the `@`-operator grammar that this
  protocol carries.

## What's new

Host dispatch adds three things on top of the existing protocol:

1. A new record kind, `host`, announced over `/control` alongside the
   existing `session` records.
2. A new snapshot endpoint, `/hosts`, parallel to `/sessions`.
3. A new dispatch kind, `host_action`, carried over the existing
   `/dispatch` endpoint.

No new endpoints. No new WebSocket. No new transport. The selector
grammar from `pool-dispatch-operator.md` is reused unchanged; only the
inventory it resolves against is different.

## Why hosts, not just agents

The existing agent dispatch addresses a *running CLI process* by
capability tags. A host dispatch addresses *a machine* by inventory:
what's installed, what's idle, what the OS is. The two are
complementary — once a host receives a `/spawn` action, the resulting
session announces itself as a normal agent and is dispatched to via
the existing agent-dispatch path.

This means callers compose the two without inventing new primitives:

```
@host {os=darwin, browsers.chrome-canary.multimodal_capable=true:5s}
  /spawn role=expert category=ocr        # creates a new agent
wait                                     # ... which announces itself

@ {nano, ocr:10s} "describe this screenshot" /done   # dispatch to it
```

## Host record

A host record is what each holo daemon announces about its own machine.
It is sent over `/control` the same way `session` registrations are
sent — the daemon registers itself as a host, then heartbeats.

```json
{
  "v": 1,
  "kind": "host",
  "instance": "fixie-mac",
  "announce_started": "2026-05-11T19:32:14Z",
  "last_seen":        "2026-05-11T20:15:03Z",

  "os":          "darwin",
  "os_version":  "24.0.0",
  "arch":        "arm64",
  "hostname":    "fixie-mac.local",

  "cpu":   { "model": "Apple M2", "cores": 8 },
  "ram_gb": 16,
  "gpu":   { "vendor": "apple", "model": "Apple M2 GPU", "cores": 10 },
  "display_present": true,

  "browsers": {
    "chrome":        { "version": "141.0.7340.30", "path": "/Applications/Google Chrome.app",        "nano_capable": true,  "multimodal_capable": false },
    "chrome-canary": { "version": "143.0.7395.0",  "path": "/Applications/Google Chrome Canary.app", "nano_capable": true,  "multimodal_capable": true  },
    "brave":         { "version": "1.71",          "path": "/Applications/Brave Browser.app",         "nano_capable": false, "multimodal_capable": false }
  },

  "tools": {
    "python": "3.12.4",
    "node":   "20.11.1",
    "ffmpeg": "6.1"
  },

  "spawn_policy": {
    "allow_visible":        true,
    "allow_headless":       false,
    "max_concurrent_tabs":  4,
    "active_tabs":          1
  },

  "capabilities": ["host:spawn", "host:inventory", "host:tunnel"]
}
```

### Load-bearing fields

- **`instance`** — stable per-machine identifier; never changes across
  daemon restarts. Used by selectors to pin to a specific host.
- **`display_present`** — WebGPU needs a real GPU context. Headless or
  no-display Chrome silently loses Nano. Any selector targeting Nano
  must filter on this; the failure mode otherwise is "browser launched,
  Nano never loads, eventually times out."
- **`browsers.<name>.nano_capable` / `multimodal_capable`** — cached
  probe results. See `## Inventory freshness` below for refresh
  semantics; treat as best-effort.
- **`spawn_policy`** — the host's own consent layer. Dispatches that
  violate the policy receive `denied`, not `error` (the host accepted
  the message but refused the action).
- **`active_tabs`** / **`max_concurrent_tabs`** — together these are
  the saturation check. A selector predicate
  `active_tabs<max_concurrent_tabs` is the canonical "pick a host that
  has room" filter.

### Inventory freshness

- **OS, hardware:** probed once at daemon startup. Cached for the
  daemon's lifetime.
- **Installed browsers (presence + version):** probed at startup;
  re-probed on `SIGHUP` or on demand via `POST /hosts/refresh`.
- **`nano_capable` / `multimodal_capable`:** lazily probed on first
  spawn that targets the browser. Cached with a 24h TTL because Chrome
  updates can flip the flag silently.
- **`active_tabs`:** updated in real time as spawns succeed / tabs
  close. Heartbeated with the host record.

A consumer should treat any field other than `instance` and `os` as
potentially stale and tolerate spawn failures gracefully via `else`.

## `/hosts` — snapshot

```
GET http://localhost:7082/hosts
```

Returns the current snapshot of all known hosts:

```json
{
  "v": 1,
  "hosts": [ /* array of host records */ ]
}
```

Parallel to the existing `/sessions` endpoint. Same caching and
freshness rules apply: this is a snapshot, not a live view. For live
updates, subscribe to `/events`.

## `/events` — host change events

The existing `/events` WebSocket gains three new event types:

```json
{ "v": 1, "type": "host_announce", "host": { /* full record */ } }
{ "v": 1, "type": "host_update",   "host": { /* full record */ } }
{ "v": 1, "type": "host_drop",     "instance": "fixie-mac", "reason": "heartbeat_timeout" }
```

`host_update` fires when any field of the record changes — most
commonly `active_tabs` ticking up/down as spawns and closures happen.

## `/dispatch` — `host_action`

The existing `/dispatch` endpoint gains a new request shape with
`kind: "host_action"`. The base `dispatch-protocol.md` rules
(versioning, request_id semantics, error codes) apply unchanged.

### Request

```json
POST /dispatch HTTP/1.1
Content-Type: application/json

{
  "v": 1,
  "kind": "host_action",
  "selector": {
    "type": "host",
    "predicates": [
      { "attr": "os",              "op": "=", "value": "darwin" },
      { "attr": "display_present", "op": "=", "value": true     },
      { "attr": "browsers.chrome-canary.multimodal_capable", "op": "=", "value": true },
      { "attr": "active_tabs",     "op": "<", "value_attr": "max_concurrent_tabs" }
    ],
    "cardinality": "one",
    "wait_ms": 5000
  },
  "action": {
    "name": "spawn",
    "params": {
      "browser":     "chrome-canary",
      "url":         "https://app-dev.tai.sh/desktop?role=expert&category=ocr",
      "window_mode": "popup",
      "label":       "ocr-expert",
      "lifecycle": {
        "idle_timeout_s":     60,
        "heartbeat_grace_s":  15
      }
    }
  },
  "request_id": "req-7f3a9c1b"
}
```

### Selector predicates

Each predicate is one of:

```json
{ "attr": "<dotted.path>", "op": "<op>", "value":      <literal>    }
{ "attr": "<dotted.path>", "op": "<op>", "value_attr": "<dotted.path>" }
```

- **`attr`** — dotted path into the host record
  (e.g. `browsers.chrome-canary.nano_capable`).
- **`op`** — one of `=`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `has`.
- **`value`** — compare to a literal.
- **`value_attr`** — compare to another field in the same host record
  (used for self-relative checks like
  `active_tabs<max_concurrent_tabs`).

Predicates are AND-ed; for OR or grouping, use the same nested-
predicate form documented in `pool-dispatch-operator.md`.

Cardinality is `"one"` (pick a single host) or `"all"` (broadcast to
every matching host — useful for "warm up the entire pool").

### Action

```json
"action": {
  "name":   "<spawn | shutdown | refresh_inventory | run>",
  "params": { /* action-specific */ }
}
```

#### `spawn`

Launches a browser window on the target host pointing at `url`. The
loaded page is responsible for announcing itself as an agent once it
boots; `spawn` only guarantees the browser process was started.

| param              | type   | required | meaning                                              |
|--------------------|--------|----------|------------------------------------------------------|
| `browser`          | string | yes      | Browser key from `host.browsers`                     |
| `url`              | string | yes      | URL to open                                          |
| `window_mode`      | string | no       | `popup` (default) \| `window` \| `tab`               |
| `label`            | string | no       | Shows in host's activity log; aids debugging         |
| `lifecycle.idle_timeout_s`     | int | no | Tab self-closes after N seconds without a prompt  |
| `lifecycle.heartbeat_grace_s`  | int | no | Broker waits N seconds for first heartbeat before declaring spawn failed |

#### `shutdown`

Closes a previously-spawned tab. The host signals the tab to call
`window.close()`; if the tab does not exit within `heartbeat_grace_s`,
the host force-kills the browser PID (logged as `forced: true`).

| param      | type   | required | meaning                          |
|------------|--------|----------|----------------------------------|
| `spawn_id` | string | yes      | The id returned from `spawn`     |

#### `refresh_inventory`

Re-runs the inventory probe on the host. Forces the cached browser
capabilities to be re-tested. Returns the updated host record.

#### `run` (deferred)

Runs a tai-script-or-shell command on the host. Reserved for a future
revision; not part of v1.

### Response

```json
{
  "v": 1,
  "request_id":    "req-7f3a9c1b",
  "status":        "dispatched",
  "host_instance": "fixie-mac",
  "spawned": {
    "spawn_id":                "sp-9c7a3e1d",
    "agent_instance_expected": "nano-expert-ocr-fixie-mac",
    "ttl_ms":                  60000
  }
}
```

For broadcast (`cardinality: "all"`), the response shape changes to an
array:

```json
{
  "v": 1,
  "request_id": "req-7f3a9c1b",
  "dispatches": [
    { "host_instance": "fixie-mac",  "status": "dispatched", "spawned": { ... } },
    { "host_instance": "lando-pi",   "status": "denied",     "detail": "allow_visible=false" }
  ]
}
```

### Status codes

Matches the agent dispatch status codes from `dispatch-protocol.md`,
extended with `denied`:

| status        | meaning                                                  |
|---------------|----------------------------------------------------------|
| `dispatched`  | host accepted; browser launching                         |
| `no_match`    | zero hosts satisfy the selector (static miss)            |
| `timeout`     | matching hosts exist but all saturated within `wait_ms`  |
| `denied`      | matched host's `spawn_policy` refused                    |
| `error`       | host accepted but the action failed; `detail` populated  |

Note: `no_match` is a *static* miss — the inventory predicate cannot
be satisfied by any currently-announced host. It returns immediately,
ignoring `wait_ms`, because waiting will not conjure a host into
existence.

`timeout` is a *dynamic* miss — matching hosts exist but `active_tabs
< max_concurrent_tabs` is currently false. The broker waits up to
`wait_ms` for capacity to free up.

## Lifecycle

### Spawn lifecycle

1. Broker receives `/dispatch` with a `spawn` action.
2. Broker resolves the selector against `/hosts` inventory.
3. Broker forwards the `spawn` to the chosen host's `/control` WS.
4. Host's local holo daemon runs the platform-equivalent of:
   ```
   open -na "Google Chrome Canary" --args --new-window \
     'https://app-dev.tai.sh/desktop?role=expert&category=ocr'
   ```
5. Host responds to the broker with `spawn_id` + `pid` (or `error`).
6. Broker responds to the original caller with the `spawn_id` and the
   expected agent `instance` name.
7. When the browser tab finishes loading, it joins `/control` as a
   normal *agent* registration. From this point it is dispatched to via
   the existing agent-dispatch path.

### Heartbeats

Two independent heartbeat tracks. Both use the existing `/control` WS
ping framing.

- **Host → broker.** The daemon heartbeats its host record (default:
  every 5s). On miss, broker emits `host_drop` and marks all spawns
  owned by the host as stale.
- **Spawned tab → broker.** The tab heartbeats as an agent
  (default: every 5s). On miss, broker emits the normal agent drop
  event; the spawning host is notified so it can update `active_tabs`.

### Shutdown paths

A spawned tab can exit four ways:

1. **Idle timeout.** Tab notices `idle_timeout_s` elapsed without a
   prompt and calls `window.close()` from inside.
2. **Explicit shutdown.** Caller sends a `shutdown` action with the
   `spawn_id`.
3. **User closes the window.** Tab's `beforeunload` fires; host
   notices the browser process exited; updates inventory.
4. **Host disappears.** Host's heartbeats stop; broker drops all the
   host's spawns at once.

Cases 1–3 are *clean*: the tab gets to deregister itself first. Case 4
is *unclean*: the broker decides unilaterally that the spawns are
gone, and the actual browser tabs may still be running — they will
realize their connection is dead and either reconnect (if the host
comes back) or terminate (if not).

## Design notes

### `request_id` is client-generated

Matches the nonce-bearing-sentinel design from `pool-dispatch-operator.md`.
Clients mint their own ids (UUIDv4 or similar), the broker is required
to dedupe on collision, and logs are correlatable end-to-end without a
server round-trip to allocate ids. Do not let the broker mint these.

### Selector predicates are structured, not stringly-typed

A predicate is `{attr, op, value}` rather than a packed string like
`"os=darwin"`. Three reasons:

1. **No string parsing on the receive side.** The broker doesn't have
   to invent and maintain a mini-parser for a format that already has
   a structured equivalent.
2. **Attr-to-attr comparisons need it.**
   `active_tabs<max_concurrent_tabs` is a self-relative check ("does
   this host have headroom") that flat strings can't naturally
   express. The `value_attr` field unlocks this without inventing
   special syntax.
3. **Mirrors the existing grammar.** `pool-dispatch-operator.md` already
   defines `attr_compare ::= identifier op value` internally — keeping
   the wire format aligned with the grammar's AST means the parser
   and the protocol share a single representation.

### No new endpoints

Reusing `/control` for host registration and `/dispatch` for host
actions, with `kind` discrimination, is intentional. It keeps the
broker's surface area small, makes host dispatch a strict extension
rather than a parallel subsystem, and means existing clients that
filter by `kind: "session"` continue to work without changes.

### Host spawn requires auth

Spawning a browser on a remote machine is a privileged operation.
Authorization uses the same SSH-cert flow as the rest of the tai /
holo ecosystem (S3-R9): hosts only accept `spawn` from clients whose
cert grants `host:spawn`. The host's `spawn_policy.allow_visible`
adds a second layer of consent — even an authorized client gets
`denied` if the host opted out of surprise tabs.

## Open / deferred

- **`run` action.** Running arbitrary tai or shell commands on the
  target host. Reserved for v2; requires more careful sandboxing
  semantics than spawn.
- **Reservation / lease.** "Hold this host for me for 5 minutes so my
  next spawn is guaranteed." Useful for multi-step orchestration
  (`spawn an OCR expert, then send it 200 screenshots over 3 minutes`)
  but adds state to the broker.
- **Host groups.** Logical pools of hosts (`@host {pool=lab-macs}`).
  Today hosts are addressed by individual attributes; group membership
  would be a label the host announces. Defer until users report
  selector pain.
- **Cost predicates.** `cost_per_hour<0.20` for cloud-hosted machines.
  v2.
