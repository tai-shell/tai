# bookmarklet/

In-page agent for the holo browser-automation primitive.

This directory holds the JS that runs in the user's browser when they click
the holo bookmark on their bookmarks bar — the page side of the channel that
the Python daemon talks to.

## Layout

- `framing.js` — JS port of `holo.framing`. Same wire format as the Python
  side; frames produced here decode cleanly on the daemon side and vice
  versa.
- `title.js` — encoder/decoder for the page → daemon channel via
  `document.title`. Two formats: `[holo:1:<base64-frame>]` (full frame) and
  `[holo:<marker>]` (short status beacon).
- `dispatch.js` — command dispatcher. Decodes a JSON command (e.g.
  `{op: "read_global", path: "R2D2_VERSION"}`) and runs it against the
  page. No `eval` / `Function` — strict-CSP origins block them.
- `core.js` — DOM wiring. `install()` creates a small fixed-position
  paste-target panel, attaches a `paste` listener that decodes frames,
  dispatches commands, and writes replies back through `document.title`.
  Idempotent on re-install; `MutationObserver` re-captures the page's
  natural title when the page itself updates it.
- `test/*.test.js` — Node `node:test` suites. Run via `npm test`.

## Roadmap

Subsequent PRs will add:

- Daemon-side channel coordinator that wires title-reader + clipboard-paste
  + framing into a single send/receive API.
- Bundling step (likely esbuild) that produces a single-file
  `javascript:`-URL payload from the ES modules.
- Walking-skeleton end-to-end test against tai.sh proving `browser.eval`
  ("read R2D2_VERSION") round-trips through the full stack.

## Notes

- Targets browsers and Node 20+ (uses `globalThis.crypto.randomUUID`,
  `atob`/`btoa`, `JSON`, `TextEncoder`).
- No build step required for tests — Node runs the source directly.
