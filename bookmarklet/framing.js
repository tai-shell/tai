// Framing protocol for the holo channel — JS port of holo.framing.
//
// The wire format is identical to the Python implementation: a sorted-key
// JSON envelope with version, session id, type, seq, total, idempotency
// id, base64-encoded data, and a CRC32 of the decoded data bytes.
// Frames produced here decode cleanly on the Python side, and vice versa.
//
// Designed to run in both browsers and Node 20+ (`globalThis.crypto`,
// `atob`, `btoa`, `JSON`, `TextEncoder`). No external dependencies.

export const PROTOCOL_VERSION = 1;

const VALID_TYPES = new Set(["cmd", "result", "ack", "ping", "pong", "bye"]);
const REQUIRED_FIELDS = ["v", "session", "type", "seq", "total", "id", "data", "crc"];

export class FrameError extends Error {
  constructor(message) {
    super(message);
    this.name = "FrameError";
  }
}

// CRC32 (IEEE 802.3, reflected, init/xorout 0xFFFFFFFF). Matches Python's
// zlib.crc32. Polynomial 0xEDB88320 is the reflected form of 0x04C11DB7.
export function crc32(bytes) {
  let crc = 0xffffffff >>> 0;
  for (let i = 0; i < bytes.length; i++) {
    crc = (crc ^ bytes[i]) >>> 0;
    for (let j = 0; j < 8; j++) {
      crc = ((crc >>> 1) ^ (0xedb88320 & -(crc & 1))) >>> 0;
    }
  }
  return ((crc ^ 0xffffffff) >>> 0).toString(16).padStart(8, "0");
}

function bytesToBase64(bytes) {
  // Build a binary string in chunks to avoid the spread-into-fromCharCode
  // limit on very large arrays.
  let s = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(s);
}

function base64ToBytes(b64) {
  const s = atob(b64);
  const bytes = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) bytes[i] = s.charCodeAt(i);
  return bytes;
}

function newId() {
  return globalThis.crypto.randomUUID();
}

function sortedJson(obj) {
  // Sort keys to match Python's json.dumps(..., sort_keys=True). Recursively
  // covers nested objects, although our envelopes are flat.
  if (Array.isArray(obj)) {
    return "[" + obj.map(sortedJson).join(",") + "]";
  }
  if (obj && typeof obj === "object") {
    const keys = Object.keys(obj).sort();
    const parts = keys.map((k) => JSON.stringify(k) + ":" + sortedJson(obj[k]));
    return "{" + parts.join(",") + "}";
  }
  return JSON.stringify(obj);
}

export function encodeFrame({
  session,
  type,
  data = new Uint8Array(),
  seq = 0,
  total = 1,
  id = newId(),
  v = PROTOCOL_VERSION,
}) {
  const envelope = {
    v,
    session,
    type,
    seq,
    total,
    id,
    data: bytesToBase64(data),
    crc: crc32(data),
  };
  return sortedJson(envelope);
}

export function decodeFrame(raw) {
  let env;
  try {
    env = JSON.parse(raw);
  } catch (e) {
    throw new FrameError(`invalid json: ${e.message}`);
  }
  if (env === null || typeof env !== "object" || Array.isArray(env)) {
    throw new FrameError("frame is not a json object");
  }
  const missing = REQUIRED_FIELDS.filter((k) => !(k in env));
  if (missing.length > 0) {
    throw new FrameError(`missing fields: ${missing.sort().join(",")}`);
  }
  if (env.v !== PROTOCOL_VERSION) {
    throw new FrameError(`unsupported version: ${env.v}`);
  }
  if (!VALID_TYPES.has(env.type)) {
    throw new FrameError(`unknown frame type: ${env.type}`);
  }
  let data;
  try {
    data = base64ToBytes(env.data);
  } catch (e) {
    throw new FrameError(`invalid base64 data: ${e.message}`);
  }
  const actualCrc = crc32(data);
  if (actualCrc !== env.crc) {
    throw new FrameError(`crc mismatch: expected ${env.crc}, got ${actualCrc}`);
  }
  return {
    v: env.v,
    session: env.session,
    type: env.type,
    seq: env.seq,
    total: env.total,
    id: env.id,
    data,
  };
}

export function chunkPayload(payload, { session, type, maxChunk = 32 * 1024 }) {
  if (!(payload instanceof Uint8Array)) {
    throw new TypeError("payload must be a Uint8Array");
  }
  if (maxChunk <= 0) {
    throw new RangeError("maxChunk must be positive");
  }
  if (payload.length === 0) {
    return [{ session, type, data: new Uint8Array(), seq: 0, total: 1, id: newId() }];
  }
  const id = newId();
  const total = Math.ceil(payload.length / maxChunk);
  const frames = [];
  for (let i = 0, seq = 0; i < payload.length; i += maxChunk, seq++) {
    frames.push({
      session,
      type,
      data: payload.subarray(i, Math.min(i + maxChunk, payload.length)),
      seq,
      total,
      id,
    });
  }
  return frames;
}

export class Reassembler {
  constructor() {
    this._buffers = new Map();
    this._totals = new Map();
    this._delivered = new Set();
  }

  feed(frame) {
    if (this._delivered.has(frame.id)) return null;
    if (!(frame.seq >= 0 && frame.seq < frame.total)) {
      throw new FrameError(`seq ${frame.seq} out of range for total ${frame.total}`);
    }
    const priorTotal = this._totals.get(frame.id);
    if (priorTotal !== undefined && priorTotal !== frame.total) {
      throw new FrameError(
        `inconsistent total for id ${frame.id}: saw ${priorTotal}, now ${frame.total}`
      );
    }
    let buf = this._buffers.get(frame.id);
    if (!buf) {
      buf = new Map();
      this._buffers.set(frame.id, buf);
    }
    if (buf.has(frame.seq)) return null;
    buf.set(frame.seq, frame.data);
    this._totals.set(frame.id, frame.total);
    if (buf.size === frame.total) {
      let totalLen = 0;
      for (let i = 0; i < frame.total; i++) totalLen += buf.get(i).length;
      const payload = new Uint8Array(totalLen);
      let offset = 0;
      for (let i = 0; i < frame.total; i++) {
        const part = buf.get(i);
        payload.set(part, offset);
        offset += part.length;
      }
      this._delivered.add(frame.id);
      this._buffers.delete(frame.id);
      this._totals.delete(frame.id);
      return payload;
    }
    return null;
  }
}
