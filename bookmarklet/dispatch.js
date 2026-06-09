// Command dispatcher for the in-page agent.
//
// The bookmarklet decodes a frame whose data is a JSON command object
// like `{op: "ping"}` or `{op: "read_global", path: "R2D2_VERSION"}`
// and calls dispatch() to execute it, returning a JSON-serializable
// result object the channel layer can encode into a reply frame.
//
// Why no `eval`/`Function`: tai.sh and many other origins ship a
// strict CSP without `'unsafe-eval'`, so dynamic code execution from
// the bookmarklet is blocked. Instead, dispatch exposes a small set
// of structured operations (read_global, read_dom, click, …) that
// cover the agent surface without crossing the eval boundary.
//
// `env` lets callers inject `window`/`document` for testability;
// when omitted, the global ones are used in a browser context.

export class DispatchError extends Error {
  constructor(message, code = "dispatch_error") {
    super(message);
    this.name = "DispatchError";
    this.code = code;
  }
}

export function dispatch(cmd, env = {}) {
  if (cmd === null || typeof cmd !== "object") {
    throw new DispatchError("command must be an object", "bad_command");
  }
  const op = cmd.op;
  if (typeof op !== "string") {
    throw new DispatchError("command must have a string `op` field", "bad_command");
  }
  const handler = HANDLERS[op];
  if (!handler) {
    throw new DispatchError(`unknown op: ${op}`, "unknown_op");
  }
  return handler(cmd, env);
}

const HANDLERS = {
  ping(_cmd, _env) {
    return { pong: true };
  },

  read_global(cmd, env) {
    if (typeof cmd.path !== "string" || cmd.path === "") {
      throw new DispatchError("read_global requires a non-empty `path`", "bad_arg");
    }
    const root = env.window ?? globalThis.window ?? globalThis;
    return { value: readPath(root, cmd.path) };
  },

  // Structured DOM query — CSP-safe, doesn't cross the eval boundary.
  // Reads either an element property (default 'innerText') or an HTML
  // attribute (when `attr` is provided).
  query_selector(cmd, env) {
    const doc = env.document ?? globalThis.document;
    if (!doc) throw new DispatchError("no document in this environment", "bad_env");
    if (typeof cmd.selector !== "string" || cmd.selector === "") {
      throw new DispatchError("query_selector requires a non-empty `selector`", "bad_arg");
    }
    const el = doc.querySelector(cmd.selector);
    if (!el) return { value: null };
    return { value: pickField(el, cmd) };
  },

  query_selector_all(cmd, env) {
    const doc = env.document ?? globalThis.document;
    if (!doc) throw new DispatchError("no document in this environment", "bad_env");
    if (typeof cmd.selector !== "string" || cmd.selector === "") {
      throw new DispatchError("query_selector_all requires a non-empty `selector`", "bad_arg");
    }
    const list = Array.from(doc.querySelectorAll(cmd.selector));
    return { value: list.map((el) => pickField(el, cmd)), count: list.length };
  },
};

function pickField(el, cmd) {
  if (typeof cmd.attr === "string" && cmd.attr !== "") {
    return el.getAttribute(cmd.attr);
  }
  const prop = typeof cmd.prop === "string" && cmd.prop !== "" ? cmd.prop : "innerText";
  // Keep results JSON-serializable. DOMRect, NodeLists, event handlers,
  // etc. would break the framing layer.
  const v = el[prop];
  if (v === null || v === undefined) return v;
  const t = typeof v;
  if (t === "string" || t === "number" || t === "boolean") return v;
  // Fall back to text for complex values; agents who want structured
  // data should use `browser_execute_js` with explicit JSON.stringify
  // instead.
  return String(v);
}

// Walks a dotted path against an object root. `cur?.[part]` would skip
// over present-but-falsy values (0, ""), so we explicitly check for
// null/undefined and otherwise continue indexing.
export function readPath(root, path) {
  const parts = path.split(".");
  let cur = root;
  for (const part of parts) {
    if (cur === null || cur === undefined) return undefined;
    cur = cur[part];
  }
  return cur;
}
