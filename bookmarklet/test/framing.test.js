import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import {
  FrameError,
  PROTOCOL_VERSION,
  Reassembler,
  chunkPayload,
  crc32,
  decodeFrame,
  encodeFrame,
} from "../framing.js";

const enc = new TextEncoder();

describe("crc32", () => {
  it("matches python zlib.crc32 reference values", () => {
    // Python: hex(zlib.crc32(b"")) == 0x0
    assert.equal(crc32(new Uint8Array()), "00000000");
    // Python: hex(zlib.crc32(b"hello")) == 0x3610a686
    assert.equal(crc32(enc.encode("hello")), "3610a686");
    // Python: hex(zlib.crc32(b"hello world")) == 0x0d4a1185
    assert.equal(crc32(enc.encode("hello world")), "0d4a1185");
    // Python: hex(zlib.crc32(bytes(range(256)))) == 0x29058c73
    assert.equal(crc32(new Uint8Array(Array.from({ length: 256 }, (_, i) => i))), "29058c73");
  });
});

describe("encodeFrame / decodeFrame", () => {
  it("round-trips a basic text payload", () => {
    const raw = encodeFrame({
      session: "s1",
      type: "cmd",
      data: enc.encode("hello world"),
    });
    const f = decodeFrame(raw);
    assert.equal(f.session, "s1");
    assert.equal(f.type, "cmd");
    assert.equal(new TextDecoder().decode(f.data), "hello world");
    assert.equal(f.seq, 0);
    assert.equal(f.total, 1);
    assert.equal(f.v, PROTOCOL_VERSION);
  });

  it("round-trips an empty payload", () => {
    const raw = encodeFrame({ session: "s", type: "ack" });
    const f = decodeFrame(raw);
    assert.equal(f.data.length, 0);
    assert.equal(f.type, "ack");
  });

  it("round-trips a full byte range", () => {
    const payload = new Uint8Array(Array.from({ length: 256 }, (_, i) => i));
    const raw = encodeFrame({ session: "s", type: "result", data: payload });
    const f = decodeFrame(raw);
    assert.deepEqual(Array.from(f.data), Array.from(payload));
  });

  it("emits sorted-key JSON for python interop", () => {
    const raw = encodeFrame({
      session: "s",
      type: "cmd",
      data: enc.encode("x"),
      seq: 0,
      total: 1,
      id: "fixed-id",
    });
    // crc(b"x") == 0x8cdc1683 per python zlib
    assert.equal(
      raw,
      '{"crc":"8cdc1683","data":"eA==","id":"fixed-id","seq":0,"session":"s","total":1,"type":"cmd","v":1}'
    );
  });
});

describe("decodeFrame errors", () => {
  it("rejects bad json", () => {
    assert.throws(() => decodeFrame("not json"), FrameError);
  });

  it("rejects non-object json", () => {
    assert.throws(() => decodeFrame("[]"), { name: "FrameError", message: /not a json object/ });
  });

  it("rejects missing fields", () => {
    assert.throws(
      () => decodeFrame(JSON.stringify({ v: 1, session: "s", type: "cmd" })),
      { name: "FrameError", message: /missing fields/ }
    );
  });

  it("rejects unknown version", () => {
    const raw = encodeFrame({ session: "s", type: "cmd", v: 99 });
    assert.throws(() => decodeFrame(raw), { name: "FrameError", message: /unsupported version/ });
  });

  it("rejects unknown type", () => {
    const env = JSON.parse(encodeFrame({ session: "s", type: "cmd" }));
    env.type = "garbage";
    assert.throws(() => decodeFrame(JSON.stringify(env)), {
      name: "FrameError",
      message: /unknown frame type/,
    });
  });

  it("rejects crc mismatch", () => {
    const env = JSON.parse(encodeFrame({ session: "s", type: "cmd", data: enc.encode("x") }));
    env.crc = "00000000";
    assert.throws(() => decodeFrame(JSON.stringify(env)), {
      name: "FrameError",
      message: /crc mismatch/,
    });
  });
});

describe("chunkPayload", () => {
  it("returns a single frame for a small payload", () => {
    const frames = chunkPayload(enc.encode("short"), { session: "s", type: "cmd" });
    assert.equal(frames.length, 1);
    assert.equal(frames[0].seq, 0);
    assert.equal(frames[0].total, 1);
    assert.equal(new TextDecoder().decode(frames[0].data), "short");
  });

  it("returns a single empty frame for empty input", () => {
    const frames = chunkPayload(new Uint8Array(), { session: "s", type: "cmd" });
    assert.equal(frames.length, 1);
    assert.equal(frames[0].data.length, 0);
    assert.equal(frames[0].total, 1);
  });

  it("splits large payloads into shared-id frames", () => {
    const payload = enc.encode("x".repeat(1000));
    const frames = chunkPayload(payload, { session: "s", type: "cmd", maxChunk: 300 });
    assert.equal(frames.length, 4);
    assert.ok(frames.every((f) => f.id === frames[0].id));
    assert.deepEqual(frames.map((f) => f.seq), [0, 1, 2, 3]);
    assert.ok(frames.every((f) => f.total === 4));
    let total = 0;
    for (const f of frames) total += f.data.length;
    assert.equal(total, 1000);
  });

  it("rejects non-Uint8Array input", () => {
    assert.throws(() => chunkPayload("string", { session: "s", type: "cmd" }), TypeError);
  });

  it("rejects zero or negative maxChunk", () => {
    assert.throws(
      () => chunkPayload(enc.encode("x"), { session: "s", type: "cmd", maxChunk: 0 }),
      RangeError
    );
  });
});

describe("Reassembler", () => {
  it("emits a single-frame message immediately", () => {
    const r = new Reassembler();
    const frames = chunkPayload(enc.encode("hi"), { session: "s", type: "cmd" });
    assert.equal(new TextDecoder().decode(r.feed(frames[0])), "hi");
  });

  it("emits multi-frame messages after the last chunk arrives", () => {
    const r = new Reassembler();
    const payload = enc.encode("x".repeat(500));
    const frames = chunkPayload(payload, { session: "s", type: "cmd", maxChunk: 100 });
    const results = frames.map((f) => r.feed(f));
    for (let i = 0; i < frames.length - 1; i++) assert.equal(results[i], null);
    assert.equal(results[results.length - 1].length, 500);
  });

  it("accepts frames out of order", () => {
    const r = new Reassembler();
    const frames = chunkPayload(enc.encode("abcdefghij"), {
      session: "s",
      type: "cmd",
      maxChunk: 2,
    });
    let last = null;
    for (let i = frames.length - 1; i >= 0; i--) last = r.feed(frames[i]);
    assert.equal(new TextDecoder().decode(last), "abcdefghij");
  });

  it("returns null when replaying a delivered message", () => {
    const r = new Reassembler();
    const frame = chunkPayload(enc.encode("once"), { session: "s", type: "cmd" })[0];
    assert.equal(new TextDecoder().decode(r.feed(frame)), "once");
    assert.equal(r.feed(frame), null);
  });

  it("returns null on duplicate chunks during reassembly", () => {
    const r = new Reassembler();
    const frames = chunkPayload(enc.encode("hello world"), {
      session: "s",
      type: "cmd",
      maxChunk: 3,
    });
    assert.equal(r.feed(frames[0]), null);
    assert.equal(r.feed(frames[0]), null);
    let last = null;
    for (let i = 1; i < frames.length; i++) last = r.feed(frames[i]);
    assert.equal(new TextDecoder().decode(last), "hello world");
  });

  it("rejects out-of-range seq", () => {
    const r = new Reassembler();
    assert.throws(
      () => r.feed({ id: "x", seq: 5, total: 2, data: new Uint8Array() }),
      { name: "FrameError", message: /out of range/ }
    );
  });

  it("rejects inconsistent totals across the same id", () => {
    const r = new Reassembler();
    r.feed({ id: "abc", seq: 0, total: 2, data: new Uint8Array([1]) });
    assert.throws(
      () => r.feed({ id: "abc", seq: 1, total: 3, data: new Uint8Array([2]) }),
      { name: "FrameError", message: /inconsistent total/ }
    );
  });

  it("handles independent messages", () => {
    const r = new Reassembler();
    const f1 = { id: "m1", seq: 0, total: 1, data: enc.encode("alpha") };
    const f2 = { id: "m2", seq: 0, total: 1, data: enc.encode("beta") };
    assert.equal(new TextDecoder().decode(r.feed(f1)), "alpha");
    assert.equal(new TextDecoder().decode(r.feed(f2)), "beta");
  });
});
