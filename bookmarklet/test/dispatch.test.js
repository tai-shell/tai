import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import { DispatchError, dispatch, readPath } from "../dispatch.js";

describe("dispatch", () => {
  it("rejects non-object commands", () => {
    assert.throws(() => dispatch(null), DispatchError);
    assert.throws(() => dispatch("ping"), DispatchError);
    assert.throws(() => dispatch(42), DispatchError);
  });

  it("rejects commands without a string `op`", () => {
    assert.throws(() => dispatch({}), { name: "DispatchError", code: "bad_command" });
    assert.throws(() => dispatch({ op: 5 }), { name: "DispatchError", code: "bad_command" });
  });

  it("rejects unknown ops", () => {
    assert.throws(
      () => dispatch({ op: "nope" }),
      { name: "DispatchError", code: "unknown_op" }
    );
  });
});

describe("dispatch: ping", () => {
  it("returns {pong: true}", () => {
    assert.deepEqual(dispatch({ op: "ping" }), { pong: true });
  });
});

describe("dispatch: read_global", () => {
  it("returns the value at a top-level path", () => {
    const env = { window: { R2D2_VERSION: "0.23" } };
    assert.deepEqual(
      dispatch({ op: "read_global", path: "R2D2_VERSION" }, env),
      { value: "0.23" }
    );
  });

  it("walks nested object paths", () => {
    const env = { window: { S3R9: { Config: { apiBase: "https://api-dev.tai.sh" } } } };
    assert.deepEqual(
      dispatch({ op: "read_global", path: "S3R9.Config.apiBase" }, env),
      { value: "https://api-dev.tai.sh" }
    );
  });

  it("returns undefined for missing paths", () => {
    const env = { window: { foo: 1 } };
    assert.deepEqual(
      dispatch({ op: "read_global", path: "bar.baz" }, env),
      { value: undefined }
    );
  });

  it("preserves falsy values", () => {
    const env = { window: { count: 0, empty: "", flag: false } };
    assert.deepEqual(dispatch({ op: "read_global", path: "count" }, env), { value: 0 });
    assert.deepEqual(dispatch({ op: "read_global", path: "empty" }, env), { value: "" });
    assert.deepEqual(dispatch({ op: "read_global", path: "flag" }, env), { value: false });
  });

  it("rejects empty or non-string path", () => {
    assert.throws(
      () => dispatch({ op: "read_global", path: "" }),
      { name: "DispatchError", code: "bad_arg" }
    );
    assert.throws(
      () => dispatch({ op: "read_global", path: 42 }),
      { name: "DispatchError", code: "bad_arg" }
    );
  });

  it("falls back to globalThis when env.window is omitted", () => {
    globalThis.__holoTestSentinel = "from-globalThis";
    try {
      const result = dispatch({ op: "read_global", path: "__holoTestSentinel" });
      assert.equal(result.value, "from-globalThis");
    } finally {
      delete globalThis.__holoTestSentinel;
    }
  });
});

describe("dispatch: query_selector", () => {
  // Minimal DOM stub that mirrors what dispatch needs: a doc with
  // querySelector / querySelectorAll returning element-shaped objects.
  function makeDoc(elements) {
    return {
      querySelector(sel) {
        return elements.find((e) => e.__sel === sel) ?? null;
      },
      querySelectorAll(sel) {
        return elements.filter((e) => e.__sel === sel);
      },
    };
  }

  it("returns innerText by default for the first match", () => {
    const env = {
      document: makeDoc([
        { __sel: "button", innerText: "Click me" },
        { __sel: "button", innerText: "Cancel" },
      ]),
    };
    assert.deepEqual(
      dispatch({ op: "query_selector", selector: "button" }, env),
      { value: "Click me" }
    );
  });

  it("returns null when nothing matches", () => {
    const env = { document: makeDoc([]) };
    assert.deepEqual(
      dispatch({ op: "query_selector", selector: ".missing" }, env),
      { value: null }
    );
  });

  it("reads a custom property via `prop`", () => {
    const env = {
      document: makeDoc([{ __sel: "h1", innerHTML: "<em>Hi</em>" }]),
    };
    assert.deepEqual(
      dispatch({ op: "query_selector", selector: "h1", prop: "innerHTML" }, env),
      { value: "<em>Hi</em>" }
    );
  });

  it("reads an HTML attribute via `attr` (takes precedence over prop)", () => {
    const env = {
      document: makeDoc([
        {
          __sel: "a.cta",
          innerText: "go",
          getAttribute(name) { return name === "href" ? "https://x/" : null; },
        },
      ]),
    };
    assert.deepEqual(
      dispatch(
        { op: "query_selector", selector: "a.cta", attr: "href", prop: "innerText" },
        env
      ),
      { value: "https://x/" }
    );
  });

  it("stringifies non-serializable values", () => {
    const env = {
      document: makeDoc([
        { __sel: "div", weird: { toString() { return "stringified"; } } },
      ]),
    };
    assert.deepEqual(
      dispatch({ op: "query_selector", selector: "div", prop: "weird" }, env),
      { value: "stringified" }
    );
  });

  it("preserves falsy primitive values", () => {
    const env = {
      document: makeDoc([{ __sel: "input", value: "", checked: false }]),
    };
    assert.deepEqual(
      dispatch({ op: "query_selector", selector: "input", prop: "value" }, env),
      { value: "" }
    );
    assert.deepEqual(
      dispatch({ op: "query_selector", selector: "input", prop: "checked" }, env),
      { value: false }
    );
  });

  it("rejects empty or non-string selector", () => {
    assert.throws(
      () => dispatch({ op: "query_selector", selector: "" }, { document: makeDoc([]) }),
      { name: "DispatchError", code: "bad_arg" }
    );
    assert.throws(
      () => dispatch({ op: "query_selector", selector: 5 }, { document: makeDoc([]) }),
      { name: "DispatchError", code: "bad_arg" }
    );
  });
});

describe("dispatch: query_selector_all", () => {
  function makeDoc(elements) {
    return {
      querySelectorAll(sel) {
        return elements.filter((e) => e.__sel === sel);
      },
    };
  }

  it("returns a list of innerText for all matches", () => {
    const env = {
      document: makeDoc([
        { __sel: "button", innerText: "OK" },
        { __sel: "button", innerText: "Cancel" },
        { __sel: "button", innerText: "Help" },
      ]),
    };
    assert.deepEqual(
      dispatch({ op: "query_selector_all", selector: "button" }, env),
      { value: ["OK", "Cancel", "Help"], count: 3 }
    );
  });

  it("returns empty list (not null) when nothing matches", () => {
    const env = { document: makeDoc([]) };
    assert.deepEqual(
      dispatch({ op: "query_selector_all", selector: ".x" }, env),
      { value: [], count: 0 }
    );
  });

  it("reads an attribute across all matches", () => {
    const env = {
      document: makeDoc([
        { __sel: "a", getAttribute: (n) => (n === "href" ? "/a" : null) },
        { __sel: "a", getAttribute: (n) => (n === "href" ? "/b" : null) },
      ]),
    };
    assert.deepEqual(
      dispatch({ op: "query_selector_all", selector: "a", attr: "href" }, env),
      { value: ["/a", "/b"], count: 2 }
    );
  });
});

describe("readPath", () => {
  it("returns root when path is empty after split", () => {
    // path.split(".") on "" gives [""], which then misses on root.
    // This is intentional — empty path means "no field," not "root".
    assert.equal(readPath({ x: 1 }, ""), undefined);
  });

  it("stops at first null/undefined ancestor", () => {
    assert.equal(readPath({ a: null }, "a.b.c"), undefined);
    assert.equal(readPath({}, "a.b"), undefined);
  });

  it("preserves intermediate falsy values that are not nullish", () => {
    assert.equal(readPath({ a: { b: 0 } }, "a.b"), 0);
    assert.equal(readPath({ a: { b: "" } }, "a.b"), "");
  });
});
