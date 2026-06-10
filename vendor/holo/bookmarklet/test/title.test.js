import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import {
  decodeFramedTitle,
  decodePlainMarker,
  encodeFramedTitle,
  encodePlainMarker,
  isHoloTitle,
} from "../title.js";

describe("encodeFramedTitle / decodeFramedTitle", () => {
  it("round-trips a frame json string", () => {
    const json = '{"v":1,"session":"s","type":"cmd"}';
    const title = encodeFramedTitle(json, "GitHub - Mozilla Firefox");
    assert.equal(decodeFramedTitle(title), json);
  });

  it("works without an original title", () => {
    const json = '{"v":1}';
    const title = encodeFramedTitle(json);
    assert.equal(title, `[holo:1:${btoa(json)}]`);
    assert.equal(decodeFramedTitle(title), json);
  });

  it("places the marker at the end so tab-strip truncation preserves the user's title", () => {
    const title = encodeFramedTitle("{}", "Important Page");
    assert.ok(title.startsWith("Important Page "));
    assert.ok(title.endsWith("]"));
  });

  it("returns null for titles without the marker", () => {
    assert.equal(decodeFramedTitle("Plain Page Title"), null);
    assert.equal(decodeFramedTitle(""), null);
  });

  it("returns null for non-string inputs", () => {
    assert.equal(decodeFramedTitle(null), null);
    assert.equal(decodeFramedTitle(undefined), null);
    assert.equal(decodeFramedTitle(42), null);
  });

  it("rejects non-string frameJson", () => {
    assert.throws(() => encodeFramedTitle(42, ""), TypeError);
  });

  it("ignores trailing whitespace before parsing", () => {
    const title = encodeFramedTitle("{}", "x") + "   ";
    assert.equal(decodeFramedTitle(title), "{}");
  });

  it("finds a marker before a browser-appended suffix", () => {
    // OS-level window titles get " - Google Chrome" / " — Firefox" added
    // by the browser. Our regex must match the marker anywhere, not just
    // at the end.
    const title = "tai.sh [holo:1:e30=] - Google Chrome";
    assert.equal(decodeFramedTitle(title), "{}");
  });

  it("returns null for malformed base64 inside the marker", () => {
    assert.equal(decodeFramedTitle("page [holo:1:!!!notbase64!!!]"), null);
  });
});

describe("encodePlainMarker / decodePlainMarker", () => {
  it("round-trips a plain marker", () => {
    const t = encodePlainMarker("cal:abc123", "My Page");
    assert.equal(t, "My Page [holo:cal:abc123]");
    assert.equal(decodePlainMarker(t), "cal:abc123");
  });

  it("rejects markers containing ']'", () => {
    assert.throws(() => encodePlainMarker("bad]marker", "x"), { message: /\]/ });
  });

  it("refuses to emit a plain marker that would parse as framed", () => {
    assert.throws(() => encodePlainMarker("1:eyJ9", "x"), { message: /'1:'/ });
  });

  it("refuses to interpret a framed marker as plain", () => {
    const framed = encodeFramedTitle("{}", "x");
    assert.equal(decodePlainMarker(framed), null);
  });

  it("returns null for titles without any marker", () => {
    assert.equal(decodePlainMarker("Just a regular title"), null);
  });

  it("finds a plain marker before a browser-appended suffix", () => {
    assert.equal(
      decodePlainMarker("tai.sh [holo:cal:abc-123] - Google Chrome"),
      "cal:abc-123"
    );
  });

  it("returns null for non-string inputs", () => {
    assert.equal(decodePlainMarker(null), null);
    assert.equal(decodePlainMarker(undefined), null);
  });

  it("rejects non-string marker", () => {
    assert.throws(() => encodePlainMarker(123, ""), TypeError);
  });
});

describe("isHoloTitle", () => {
  it("recognizes both framed and plain forms", () => {
    assert.equal(isHoloTitle(encodeFramedTitle("{}", "x")), true);
    assert.equal(isHoloTitle(encodePlainMarker("cal:1", "x")), true);
  });

  it("rejects regular titles", () => {
    assert.equal(isHoloTitle("Just a page title"), false);
    assert.equal(isHoloTitle(""), false);
  });
});
