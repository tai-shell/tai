# Agent Dispatch Operator — Grammar and Semantics

A bash-extension operator for dispatching prompts to one or more AI
agent CLI instances running in their own xterms (Droids), with
predicate-based selection, timeout-bounded waiting, and self-announced
completion.

## Heritage

This is `expect/send` generalized. Where `expect` controls one process
via a single PTY, this addresses N PTYs by capability or label, with
mDNS-based discovery (via holo) and a bash-flavored host language. The
shell does **not** own each agent's lifecycle — agents are long-lived
REPLs in their own terminals. The operator injects keystrokes and
watches for a self-announced completion sentinel; it never spawns,
waits-on, or reaps the agent process.

## Two operators, one model

- `@` — *address an agent.* Followed by an optional pool selector and
  a prompt. The prompt is dispatched to the matching agent's terminal
  via PTY injection.
- `{...}` and `[...]` — *pool selectors.* `{}` picks one matching
  agent; `[]` broadcasts to every matching agent. `@` is the verb;
  `{}` / `[]` is the addressee.

### Why braces and brackets

- `{...}` denotes a *set* — pick one. Order doesn't matter.
- `[...]` denotes an *ordered collection* — broadcast preserves
  iteration order if needed.
- The cardinality of the dispatch (one vs. many) is visible at a
  glance from the bracket shape.

### Why `@`

- Reads as "addressing" — matches the GitHub/Slack/Twitter convention.
- No collision with bash semantics: `$@`, `${var[@]}`, `${var@Q}` all
  require a `$` or `[` first; `@(pattern)` extglob only appears at
  argument position.
- Absolute-path commands (`/usr/bin/foo`) keep working — `@` does not
  shadow the `/`-prefix everyday users rely on.

## Surface grammar

```
dispatch       ::= "@" [ pool ] [ prompt ] [ sentinel ] [ "else" action ]
                 | "@" [ pool ] "do" line { line } "end"

pool           ::= "{" selector [ ":" wait_config ] "}"      (* pick one *)
                 | "[" selector [ ":" wait_config ] "]"      (* broadcast *)
                 | (omitted)                                  (* uses $AI_AGENT *)

selector       ::= "*"
                 | predicate_or
predicate_or   ::= predicate_and { "or" predicate_and }
predicate_and  ::= predicate { "," predicate }
predicate      ::= [ "!" ] atom
atom           ::= tag | attr_compare | "(" predicate_or ")"

tag            ::= identifier
attr_compare   ::= identifier op value
op             ::= "=" | "!=" | "<" | "<=" | ">" | ">="
value          ::= number | string | version | duration | size

wait_config    ::= wait_term { ";" wait_term }              (* ; not , — see below *)
wait_term      ::= duration | scheduling_hint
duration       ::= "0" | "inf" | <number><unit>
unit           ::= "ms" | "s" | "m" | "h"

prompt         ::= bash_word_list                           (* normal expansion *)
                 | here_doc

sentinel       ::= "/" identifier                           (* default: /done *)

action         ::= bash_command_list                        (* incl. nested @ *)
line           ::= prompt [ sentinel ]                      (* inside do…end *)
```

`;` is the separator inside `wait_config` rather than `,` so the
selector's AND-comma stays unambiguous: `{coding:5s;fifo}` is *coding,
five-second wait, FIFO*; `{coding,gpu:5s}` is *coding AND gpu, five-
second wait*. The colon partitions selector from wait config.

## Selector semantics

| Form                              | Meaning                                  |
| --------------------------------- | ---------------------------------------- |
| `{*}`                             | any agent (pick one)                     |
| `[*]`                             | every agent (broadcast)                  |
| `{coding}`                        | tagged `coding`                          |
| `{!coding}`                       | not tagged `coding`                      |
| `{coding, gpu}`                   | tagged both (AND)                        |
| `{coding or research}`            | tagged either (OR)                       |
| `{model=claude-sonnet-4}`         | attribute equality                       |
| `{context>=200k}`                 | attribute comparison                     |
| `{(gpt or claude), region=us}`    | grouped OR with AND                      |
| `[chrome]`                        | every chrome instance                    |

Predicates are evaluated at dispatch time (snapshot semantics). An
agent that satisfies `queue_depth<5` at pick time may exceed it during
generation; selection predicates are not runtime invariants.

## Wait configuration

| Form              | Meaning                                                  |
| ----------------- | -------------------------------------------------------- |
| `{sel}`           | wait up to the registry's configured pool default        |
| `{sel:0}`         | don't wait — dispatch only if a free agent exists *now*  |
| `{sel:5s}`        | wait up to 5 seconds                                     |
| `{sel:5s;fifo}`   | wait up to 5s, FIFO scheduling among waiters             |
| `{sel:inf}`       | wait forever (explicit; no implicit forever-wait)        |

`{sel}` (no wait config) does **not** default to forever. The registry
declares a per-pool timeout; `{sel}` inherits it. Implicit forever-
waits are a reliability foot-gun in networked / interactive contexts;
`:inf` exists for the rare case where it's actually wanted.

## Failure modes

- **Static-attribute miss.** No agent can ever satisfy the selector
  (e.g. `model=claude-sonnet-99`). Fail immediately; ignore timeout.
  Waiting cannot conjure an agent that does not exist.
- **Dynamic-attribute miss.** Agents could match but currently don't
  (e.g. all matches are over rate-limit). Wait up to timeout — the
  metric may change.
- **Pool saturation.** Matching agents exist but all are busy
  (mid-generation, REPL not yet at ready state). Wait up to timeout.
- **Sentinel timeout.** The agent accepted the prompt but did not
  emit the sync sentinel within the wait_config window. Treated as a
  timeout; `else` action fires.
- **Dynamic-attribute unknown.** A required dynamic attribute has no
  current value (collector hasn't reported yet, agent just joined).
  Treated as not-yet-satisfied (wait), to avoid optimistic dispatches
  that may never complete.

Whether an attribute is static or dynamic is determined by the agent's
declaration in the registry, not by selector syntax. (See "Open /
deferred" — making the static/dynamic split visible at the call site
is on the table.)

## Sync sentinel — `/done` and friends

A trailing `/identifier` after the prompt is a *sync sentinel*: it
tells the runtime to consider the dispatch complete when the agent
emits a matching marker line in its terminal.

```
@ {research:30s} "summarize $url" /done
```

Compiles to roughly:

1. Pick a free agent matching `{research}`.
2. Generate a unique nonce (e.g. `7f3a9c1b`).
3. Inject the user's prompt as keystrokes, with a system-instruction
   prefix:

   ```
   <user prompt>
   When this task is fully complete, print exactly:
       /done:7f3a9c1b
   on its own line.
   ```

4. Watch the agent's PTY (via the asciicast-recorder slice) for the
   line `/done:7f3a9c1b`.
5. On match → dispatch complete; agent is "free" again.
6. On wait_config timeout without match → `else` fires.

Without a sentinel, dispatch is fire-and-forget: the runtime hands
off keystrokes and returns control immediately. There is no way to
detect completion or block on it.

`/done` is the canonical name; users may pick any identifier. Multiple
sentinels (e.g. `/progress` for streaming events, `/error` for
explicit failure) are reserved for v2.

### Why printable + nonce, not a non-printable byte

Plain printable ASCII with a unique per-dispatch nonce eliminates the
collision risk that motivates non-printable markers in the first
place. Non-printables have several real downsides:

- PTYs interpret most C0 control bytes (`\x07` is bell, `\x1b` starts
  escape sequences, `\x0d` resets the cursor). Most of the C0 range
  *does something* when it reaches a terminal.
- LLMs do not reliably emit specific bytes when instructed. They
  produce the name of the character, an escape sequence, or skip it.
- Non-printables are invisible in playback, scroll-back, and grep
  output — debugging "did the marker actually arrive" becomes hard.

A nonce-bearing printable sentinel is collision-free without
inheriting any of those problems.

## Stickiness — the `do…end` block

Within a `do…end` block scoped to an `@` dispatch, every `@` (or
bare prompt line) inside addresses the *same* agent that was selected
at the top:

```
@ {research:5s} do
  "Determine if github repo X supports our use cases" /done
  "Find alternate repos with similar features" /done
  "Summarize the comparison in three bullets" /done
end
```

The selector picks one agent at the top; subsequent prompts inside
the block bypass the selector and route to that pinned instance.
Outside the block, normal selector semantics resume.

If the pinned agent becomes unavailable mid-block (crash, kill,
disconnect), the block fails immediately at the next prompt
regardless of the original wait config — there is no implicit
re-pick. Users who want failover must structure the block with an
explicit `else`:

```
@ {research:5s} do
  "..." /done
  "..." /done
end else log "research session lost"
```

## Output capture

The dispatch operator does not capture stdout — agent output streams
to the agent's own terminal. For scripts that need to *read* an
agent's response, three conventions exist, in increasing order of
cleanliness:

1. **Asciicast scrape.** The runtime records each agent's PTY in
   asciicast v2 format with a rolling buffer
   (`asciicast-recorder.js`). The shell can slice the recording
   from dispatch-start to sentinel-arrival, strip ANSI escape
   sequences, and return the captured text. Universal but inherits
   terminal-formatting messiness (line wrap, partial redraws,
   cursor-positioning).

2. **Marker-delimited extraction.** Rewrite the prompt to wrap the
   machine-readable answer between unique markers:

   ```
   @ {research:30s} "summarize $url. Wrap your answer in
     <<<ANS:NONCE>>>...<<<END:NONCE>>>." /done
   ```

   Script extracts the substring between markers from the asciicast
   slice. Robust to formatting quirks; verbose at the call site.

3. **Side-channel via holo.** MCP-aware agents (Claude Code, Codex
   CLI) can emit structured tool results. The runtime subscribes to
   a holo channel keyed on the dispatch nonce; the agent posts its
   answer through an MCP tool call. Cleanest for structured data;
   only available for MCP-aware agents.

The runtime should provide a builtin (e.g. `@capture`) that uses #1
by default and #3 when available. v1 commits to #1 and #2; #3 is
opt-in per agent.

## Worked examples

```
# Single dispatch, default agent (from $AI_AGENT)
@ /usage

# Pick a coding agent, fire-and-forget
@ {coding} "lint this file: $file"

# Bounded-concurrency loop — fan iterations across all chrome droids
for url in $(cat list); do
  @ {chrome:30s} "open $url and report the title" /done else log "skip $url"
done
wait                            # block until every outstanding /done lands

# Same-agent conversation continuation
@ {research:5s} do
  "Summarize the differences between repo X and repo Y" /done
  "Of those, which would matter most to a small team?" /done
  "Output the answer as JSON: {choice, reason}" /done
end

# Strong-then-cheap fallback
@ {model=claude-sonnet-4:0} "$task" /done else \
  @ {model=claude-haiku-4:5s} "$task" /done

# Broadcast — ask every coding agent the same question
@ [coding:10s] "what's wrong with this code?" /done

# Heredoc prompt with shell expansion
@ {research:30s} <<END /done
Compare the security postures of $repo_a and $repo_b.
Cite specific commit hashes from the last 30 days.
END
```

## Default selector — `$AI_AGENT`

A bare `@ <prompt>` (no pool clause) uses the selector in `$AI_AGENT`,
which holds a *selector expression*, not just a name:

```
AI_AGENT='{model=claude-sonnet-4, region=us}'
@ "what's the time?"          # uses $AI_AGENT
@ {model=gpt-5} "..."         # explicit override per call
```

Because `{` and `}` are bash brace-expansion metacharacters, the
selector must be quoted on assignment. The shell fork's parser
recognizes the quoted form when re-injecting `$AI_AGENT` into a
dispatch.

## Pipelines and prompt expansion

- The prompt goes through normal bash expansion (variables, command
  substitution, quoting, heredocs) before injection. Word-split
  tokens are rejoined with single spaces — agents see one string,
  not an argv.
- `cat foo | @ {sel} "..."` means *paste the contents of `foo` as
  keystrokes into the selected agent's PTY, then send the prompt*.
  Bracketed-paste mode is used where the agent's terminal supports
  it.

## Sync barrier

A bare `wait` (analogous to bash's job-control `wait`) blocks until
every outstanding sentinel has been received. Combined with the
fire-and-forget loop pattern, this gives clean barrier semantics:

```
for url in $(cat list); do
  @ {chrome:30s} "..." /done
done
wait                            # all chrome dispatches have landed /done
```

`wait <selector>` is reserved for v2 (block until all dispatches
matching the selector complete).

## Design decisions locked

1. **Static vs. dynamic attribute declaration.** Source of truth is
   the registry (holo announce). Selectors do not distinguish
   syntactically; failure mode is determined by the registry.
2. **Strict default on timeout.** Unmet wait_config raises an error
   unless an `else` clause is present. `else skip` to drop silently.
3. **`{}` picks one; `[]` broadcasts to all.** Both are v1.
4. **Sentinel-based completion detection.** No regex on per-agent
   prompt patterns. The agent self-announces via `/done`.
5. **Stickiness is explicit via `do…end`.** No implicit sticky-
   routing across separate `@` calls.
6. **`{sel}` defaults to the registry pool default**, not forever.
   `:inf` exists for explicit forever-waits.

## Open / deferred

- **Static / dynamic call-site visibility.** Whether to mark dynamic
  attributes syntactically (e.g. `@cpu<70`) so a reader can predict
  the failure mode without consulting the registry. Defer until users
  report selector debugging pain.
- **Cost-weighted ordering.** `{coding order_by cost}`. Selectors
  with cost predicates (`cost_per_mtok<0.05`) cover most cases; the
  ordering construct is deferred.
- **Capacity overrides.** `{coding:4;5s}` — "use up to 4 matches"
  inside a single dispatch. Syntax not finalized; may conflict with
  the duration parser.
- **Soft constraints.** `{gpu?, cpu:5s}` — "prefer GPU, accept CPU."
  v2.
- **Multiple sentinels.** `/progress`, `/error` for streaming /
  failure events. v2.
- **Named pools.** `{pool:east, gpu}`. v2; assumes single ambient
  pool today.
- **`wait <selector>`.** Barrier scoped to a subset of outstanding
  dispatches. v2.

## Notes for the shell fork

- The `@` operator MUST scope to interactive REPL input or
  explicitly-marked agent-script regions. A library `.sh` containing
  `@ {...} "..."` should not silently fire dispatches when sourced
  by a regular bash script. (The fork's parser tracks "interactive
  command-line" vs. "sourced file body" already.)
- `set -o posix` should disable the `@` operator entirely; it is not
  POSIX.
- `@`-line content goes through normal bash expansion. Quoting
  semantics match bash exactly: `'$VAR'` literal, `"$VAR"` expanded.
- The pool selector itself is parsed *after* shell quoting, so
  `@ {model="gpt-4"} "..."` works the obvious way.
- Per-invocation env override: `AI_AGENT='{...}' @ "..."` should
  work, matching the existing `VAR=val command args` idiom. The
  parser's command-lookup hook needs to recognize `@` here.
