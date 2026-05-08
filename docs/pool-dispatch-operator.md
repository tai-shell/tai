# Pool Dispatch Operator — Grammar and Semantics

A grouping-symbol operator for dispatching inputs to a bounded pool of
worker resources, with predicate-based selection and timeout-bounded
waiting.

## Overview

The operator uses brace syntax `{ ... }` to denote a *pool* of workers
matching a selector. A statement of the form:

```
{<selector>:<wait-config>} <input> [else <action>]
```

picks one worker from the pool that satisfies the selector and dispatches
`<input>` to it, waiting up to the configured time if no worker is
currently free.

### Why braces

- `{}` denotes a *set* in mathematics — unordered, no positional meaning.
  This matches a worker pool: you don't care *which* of several
  interchangeable workers takes the job, only that one is free.
- `[]` implies an ordered/indexed sequence, which would be misleading.
- `()` implies an ordered tuple or function application.

This leaves `[*]` free for a sibling fan-out operator (broadcast to all
workers), should it be needed later.

## Grammar (EBNF-ish)

```
dispatch        ::= pool_expr input [ "else" action ]

pool_expr       ::= "{" selector [ ":" wait_config ] "}"

selector        ::= "*"                              (* match any *)
                  | predicate_or

predicate_or    ::= predicate_and { "or" predicate_and }
predicate_and   ::= predicate { "," predicate }
predicate       ::= [ "!" ] atom
atom            ::= tag
                  | attr_compare
                  | "(" predicate_or ")"

tag             ::= identifier                       (* bare tag = membership *)
attr_compare    ::= identifier op value
op              ::= "=" | "!=" | "<" | "<=" | ">" | ">="
value           ::= number | string | version | duration | size

wait_config     ::= wait_term { "," wait_term }
wait_term       ::= duration                         (* 0, 5s, 2m, ... *)
                  | scheduling_hint                  (* fifo, lifo, ... *)

duration        ::= "0" | <number><unit>             (* unit: ms, s, m, h *)
input           ::= <whatever the host language uses>
action          ::= <statement in host language>
```

## Semantics

### Selector evaluation

| Form                      | Meaning                             |
| ------------------------- | ----------------------------------- |
| `{*}`                     | any resource                        |
| `{audio}`                 | has tag `audio`                     |
| `{!audio}`                | does not have tag `audio`           |
| `{audio, video}`          | has both tags (AND)                 |
| `{audio or video}`        | has either tag (OR)                 |
| `{java>=11}`              | attribute `java` >= 11              |
| `{audio, cpu<70}`         | tagged audio AND cpu under 70%      |
| `{(gpu or tpu), mem>2GB}` | accelerator present AND >2GB memory |

### Wait configuration

| Form            | Meaning                                      |
| --------------- | -------------------------------------------- |
| `{sel}`         | wait forever for a match                     |
| `{sel:0}`       | don't wait — dispatch only if free right now |
| `{sel:5s}`      | wait up to 5 seconds                         |
| `{sel:5s,fifo}` | wait up to 5s, FIFO scheduling among waiters |

### Failure modes

- **Static-attribute miss** — no worker can ever satisfy the selector
  (e.g. `java>=11` when no such worker exists in the pool). Fail
  immediately; ignore the timeout. Waiting cannot conjure a worker that
  doesn't exist.
- **Dynamic-attribute miss** — workers exist that *could* satisfy the
  selector, but currently don't (e.g. all matching workers have
  `cpu>=70`). Wait up to the timeout, since dynamic metrics may change.
- **Pool saturation** — matching workers exist but all are busy. Wait up
  to the timeout.

Whether an attribute is static or dynamic is determined by its
declaration on the worker, not by selector syntax.

### Predicate evaluation timing

Predicates are evaluated at **dispatch time** (snapshot semantics). A
worker that satisfies `cpu<70` at the moment it is picked may exceed
that threshold during execution; this is *not* enforced mid-job.
Selection predicates are not runtime invariants.

### Timeout action

- With `else <action>`: on timeout, run the action and continue.
- Without `else`: on timeout, raise an error and abort. Strict by
  default. Use explicit `else skip` to drop silently.

This default prevents accidental silent data loss.

## Worked examples

```
{*} $url                                  # any worker, wait forever
{*:5s} $url else log "dropped"            # any worker, 5s patience, drop on timeout
{*:0} $url else queue $url                # try-once, queue if all busy

{audio:5s} $clip                          # tag-filtered pool
{java>=11, mem>2GB:30s} $build else fail  # capability-gated
{gpu, cpu<70:5s,fifo} $infer              # mixed static + dynamic, FIFO waiting
{gpu:0} $infer else cpu_fallback $infer   # GPU if free, else CPU
{!busy, region=eu:10s} $req               # negation + equality
{(gpu or tpu), mem>2GB:5s} $job           # grouped OR
```

A typical loop:

```
for url in $urls; do
  {*:5s} $url else log "no worker for $url"
done
```

Reads as: *"from the pool of all workers, dispatch `$url`; wait up to
5 seconds; if no worker frees up, log and move on."*

## Design decisions to lock in before extending

1. **Static vs. dynamic attribute declaration on workers.** This drives
   failure-mode behavior (fail-fast vs. wait) and must be known by the
   runtime before the selector can be evaluated correctly.
2. **Default timeout policy.** Strict (error on timeout) is the
   recommended default; lenient (silent drop) requires explicit opt-in
   via `else skip`. Changing this default later is user-visible and
   risky.

## Reserved for later versions

- **Preference vs. requirement.** A `?` marker for soft constraints,
  e.g. `{gpu?, cpu:5s}` meaning "prefer GPU, accept CPU." Not in v1.
- **Capacity overrides.** A numeric position in the selector for "use
  at most N matching workers," e.g. `{audio:4, 5s}`. Syntax not yet
  finalized; may conflict with duration parsing.
- **Cost or weight in selection.** Ordering hints among matching
  workers, e.g. `{gpu order_by cost:5s}`. Not in v1.
- **Sibling fan-out operator `[*]`.** Broadcast the input to *every*
  matching worker rather than dispatching to one. Different operator,
  different semantics; reserved for a future addition.
