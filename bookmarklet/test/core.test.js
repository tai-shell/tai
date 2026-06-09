// Unit tests for core.js. The popup-window plumbing is exercised
// through a hand-rolled DOM stub — small enough that pulling in
// jsdom isn't worth the dev-dependency. End-to-end paste/reply
// behavior against a real browser is covered by the integration
// demo (`holo demo`).

import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import {
  buildPopupBody,
  installInPopup,
  stripHoloMarker,
} from "../core.js";
import { encodeFrame } from "../framing.js";

// ---------- DOM stub ----------

function stubElement(tagName) {
  const listeners = {};
  return {
    tagName,
    children: [],
    style: {},
    attributes: {},
    listeners,
    contentEditable: "false",
    spellcheck: true,
    textContent: "",
    value: "",
    setAttribute(k, v) {
      this.attributes[k] = v;
    },
    addEventListener(name, fn) {
      (listeners[name] ||= []).push(fn);
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    focus() {
      this._focused = true;
    },
    fireEvent(name, event = {}) {
      for (const fn of listeners[name] ?? []) fn(event);
    },
  };
}

function stubWindow() {
  const titleEl = stubElement("title");
  titleEl.textContent = "";
  const body = stubElement("body");
  const doc = {
    body,
    _titleEl: titleEl,
    get title() {
      return titleEl.textContent;
    },
    set title(v) {
      titleEl.textContent = v;
      titleEl.fireEvent("childList");
    },
    querySelector(sel) {
      if (sel === "title") return titleEl;
      return null;
    },
    createElement(tag) {
      // iframes need a `contentWindow` test hook so tests can capture
      // postMessages going to the agent iframe.
      const el = stubElement(tag);
      if (tag === "iframe") {
        el.contentWindow = {
          posted: [],
          postMessage(data, origin) {
            this.posted.push({ data, origin });
          },
        };
      }
      return el;
    },
  };
  const win = {
    document: doc,
    listeners: {},
    location: {
      origin: "https://host.example",
      _replaced: null,
      replace(url) {
        this._replaced = url;
      },
    },
    URL: globalThis.URL,
    addEventListener(name, fn) {
      (this.listeners[name] ||= []).push(fn);
    },
    removeEventListener(name, fn) {
      this.listeners[name] = (this.listeners[name] ?? []).filter((x) => x !== fn);
    },
    postMessage(data, origin) {
      (this._posted ||= []).push({ data, origin });
    },
    setTimeout: (fn) => fn(),
    MutationObserver: class {
      constructor(cb) {
        this._cb = cb;
      }
      observe(target) {
        target.addEventListener("childList", () => this._cb());
      }
    },
  };
  return win;
}

// ---------- pure helpers ----------

describe("stripHoloMarker", () => {
  it("removes a trailing framed marker", () => {
    assert.equal(stripHoloMarker("Page Title [holo:1:eyJ9]"), "Page Title");
  });

  it("removes a trailing plain marker", () => {
    assert.equal(stripHoloMarker("Page Title [holo:cal:abc]"), "Page Title");
  });

  it("returns titles without markers unchanged", () => {
    assert.equal(stripHoloMarker("Plain Page Title"), "Plain Page Title");
  });

  it("trims surrounding whitespace", () => {
    assert.equal(
      stripHoloMarker("  Page Title   [holo:cal:1]  "),
      "Page Title",
    );
  });

  it("returns empty string for non-string input", () => {
    assert.equal(stripHoloMarker(null), "");
    assert.equal(stripHoloMarker(undefined), "");
    assert.equal(stripHoloMarker(42), "");
  });

  it("returns empty string when the title is just a marker", () => {
    assert.equal(stripHoloMarker("[holo:cal:1]"), "");
  });

  it("only strips a trailing marker, not embedded brackets earlier", () => {
    assert.equal(
      stripHoloMarker("[holo:something] in middle of title"),
      "[holo:something] in middle of title",
    );
  });
});

describe("core.js module exports", () => {
  it("exports install, installInPopup, buildPopupBody, stripHoloMarker", async () => {
    const mod = await import("../core.js");
    assert.equal(typeof mod.install, "function");
    assert.equal(typeof mod.installInPopup, "function");
    assert.equal(typeof mod.buildPopupBody, "function");
    assert.equal(typeof mod.stripHoloMarker, "function");
  });
});

// ---------- buildPopupBody ----------

describe("buildPopupBody", () => {
  it("creates a textarea paste target inside the body", () => {
    const win = stubWindow();
    const { textarea } = buildPopupBody(win.document);
    assert.equal(textarea.tagName, "textarea");
    assert.equal(win.document.body.children[0], textarea);
    assert.equal(textarea.spellcheck, false);
    assert.equal(textarea.attributes["aria-label"], "holo paste target");
    assert.equal(textarea.attributes["id"] ?? textarea.id, "__holo_paste_target__");
  });

  it("creates a QR canvas as a sibling of the textarea", () => {
    const win = stubWindow();
    const { canvas } = buildPopupBody(win.document);
    assert.equal(canvas.tagName, "canvas");
    assert.equal(win.document.body.children[1], canvas);
    assert.equal(canvas.attributes["aria-label"], "holo reply channel");
    assert.ok(canvas.width > 0);
    assert.equal(canvas.width, canvas.height);
  });

  it("sets the popup document title", () => {
    const win = stubWindow();
    buildPopupBody(win.document);
    assert.equal(win.document.title, "holo console");
  });

  it("seeds the textarea with a 'keep this window open' note", () => {
    const win = stubWindow();
    const { textarea } = buildPopupBody(win.document);
    assert.match(textarea.value, /keep this window open/);
  });
});

// ---------- installInPopup ----------

describe("installInPopup", () => {
  it("emits a calibration beacon as a plain marker on the popup title", () => {
    const popup = stubWindow();
    const opener = stubWindow();
    const sid = "abc-123";
    installInPopup(popup, opener, sid);
    assert.match(popup.document.title, /\[holo:cal:abc-123\]$/);
  });

  it("emits a bye marker on pagehide", () => {
    const popup = stubWindow();
    const opener = stubWindow();
    installInPopup(popup, opener, "sid-1");
    popup.listeners.pagehide?.[0]?.();
    assert.match(popup.document.title, /\[holo:bye:sid-1\]$/);
  });

  it("registers a paste listener on the textarea", () => {
    const popup = stubWindow();
    const opener = stubWindow();
    installInPopup(popup, opener, "sid");
    const textarea = popup.document.body.children[0];
    assert.ok(textarea.listeners.paste?.length >= 1);
  });

  it("focuses the textarea on install", () => {
    const popup = stubWindow();
    const opener = stubWindow();
    installInPopup(popup, opener, "sid");
    const textarea = popup.document.body.children[0];
    assert.equal(textarea._focused, true);
  });
});

// ---------- ws_handshake op (cross-origin popup navigation) ----------

import { installHostListener } from "../core.js";

describe("ws_handshake op (popup navigation)", () => {
  function buildHandshakeFrame(session, url, token) {
    return encodeFrame({
      session,
      type: "cmd",
      data: new TextEncoder().encode(
        JSON.stringify({ op: "ws_handshake", url, token }),
      ),
      id: "frame-1",
    });
  }

  function deliverPaste(popup, frameJson) {
    const textarea = popup.document.body.children[0];
    textarea.fireEvent("paste", {
      clipboardData: { getData: () => frameJson },
      preventDefault() {},
      stopPropagation() {},
    });
  }

  it("opens a separate ws popup at the daemon URL, leaving the about:blank popup intact", () => {
    const popup = stubWindow();
    const opener = stubWindow();
    // Stand in for a host-side __holo state object so handleWsHandshake
    // can register the ws popup back on it.
    opener.__holo = { session: "sid-x", popup, wsPopup: null };
    const opened = [];
    popup.open = (url, name, features) => {
      opened.push({ url, name, features });
      return stubWindow();
    };
    installInPopup(popup, opener, "sid-x");

    deliverPaste(
      popup,
      buildHandshakeFrame("sid-x", "http://127.0.0.1:1234/popup.html", "TOK"),
    );

    // The about:blank popup must NOT have navigated — the QR fallback
    // path depends on its paste handler + canvas staying alive.
    assert.equal(popup.location._replaced, null, "about:blank popup was navigated; QR fallback would break");

    // A separate ws popup was opened at the daemon URL.
    assert.equal(opened.length, 1);
    assert.equal(opened[0].name, "holo_ws_popup");
    assert.match(opened[0].url, /^http:\/\/127\.0\.0\.1:1234\/popup\.html#/);
    assert.match(opened[0].url, /sid=sid-x/);
    assert.match(opened[0].url, /token=TOK/);
    assert.match(opened[0].url, /parentOrigin=https%3A%2F%2Fhost\.example/);

    // ws popup got registered on the host so dispatch routing accepts its postMessages.
    assert.ok(opener.__holo.wsPopup);
  });
});

describe("installHostListener (ws-popup → host dispatch relay)", () => {
  function setup({ session = "sid-1", wsPopupRef } = {}) {
    const host = stubWindow();
    const wsPopup = wsPopupRef ?? stubWindow();
    const state = { session, popup: stubWindow(), wsPopup };
    installHostListener(host, state);
    return { host, wsPopup, state };
  }

  it("runs dispatch on a holo-popup cmd and posts the result back", () => {
    const { host, wsPopup } = setup();
    host.R2D2_VERSION = "1.2.3";

    fireMessage(host, "http://127.0.0.1:1234", {
      source: "holo-popup",
      type: "cmd",
      session: "sid-1",
      id: "frame-A",
      cmd: { op: "read_global", path: "R2D2_VERSION" },
    }, wsPopup);

    const posts = wsPopup._posted ?? [];
    assert.equal(posts.length, 1);
    assert.equal(posts[0].origin, "http://127.0.0.1:1234");
    assert.equal(posts[0].data.source, "holo-host");
    assert.equal(posts[0].data.type, "result");
    assert.equal(posts[0].data.id, "frame-A");
    assert.deepEqual(posts[0].data.result, { value: "1.2.3" });
  });

  it("ignores messages whose source isn't the registered ws popup", () => {
    const { host, wsPopup } = setup();
    const stranger = stubWindow();

    fireMessage(host, "http://attacker.example", {
      source: "holo-popup",
      type: "cmd",
      session: "sid-1",
      id: "frame-evil",
      cmd: { op: "ping" },
    }, stranger);

    assert.equal((wsPopup._posted ?? []).length, 0);
  });

  it("ignores messages with a mismatched session id", () => {
    const { host, wsPopup } = setup({ session: "sid-mine" });

    fireMessage(host, "http://127.0.0.1:1234", {
      source: "holo-popup",
      type: "cmd",
      session: "sid-other",
      id: "frame-mismatch",
      cmd: { op: "ping" },
    }, wsPopup);

    assert.equal((wsPopup._posted ?? []).length, 0);
  });

  it("returns a structured error when dispatch throws", () => {
    const { host, wsPopup } = setup();

    fireMessage(host, "http://127.0.0.1:1234", {
      source: "holo-popup",
      type: "cmd",
      session: "sid-1",
      id: "frame-err",
      cmd: { op: "read_global", path: "" },
    }, wsPopup);

    const posts = wsPopup._posted ?? [];
    assert.equal(posts.length, 1);
    assert.ok(posts[0].data.result.error);
    assert.equal(posts[0].data.result.error.code, "bad_arg");
  });
});

function fireMessage(host, origin, data, source = null) {
  for (const fn of host.listeners.message ?? []) {
    fn({ origin, data, source });
  }
}
