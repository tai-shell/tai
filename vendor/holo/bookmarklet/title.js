// Title-channel codec for the page → daemon direction.
//
// The bookmarklet writes responses and beacons into `document.title`,
// which the daemon reads via OS window-title APIs. Two title formats:
//
//   `<originalTitle> [holo:1:<base64-frame-json>]`
//        "framed" form — carries an encodeFrame() payload.
//   `<originalTitle> [holo:<marker>]`
//        "plain" form — short status beacon (cal, bye, err:...).
//
// Markers live at the *end* of the title so that browser tab strips
// (which truncate from the right) keep the user's original title
// visible. Window-title OS APIs return the full string regardless.
// `<originalTitle>` is captured at install and never modified — the
// page is free to update its own title; we re-append on each write.

// Browsers append "- Google Chrome" / "— Firefox" / etc. to the OS-level
// window title, so our marker (written at the end of document.title) ends
// up in the middle of the OS title the daemon reads. Match anywhere; only
// one marker is present per title in practice.
const FRAMED_RE = /\[holo:1:([A-Za-z0-9+/=]+)\]/;
const PLAIN_RE = /\[holo:([^\]]+)\]/;

// btoa / atob only handle Latin1 (code points 0–255). Frame envelopes
// or session ids that ever contain a non-Latin1 character — e.g., a
// Unicode ellipsis from macOS visually truncating a long window title,
// or a Unicode char in the host page's original title — would crash
// btoa with InvalidCharacterError. Round-trip via UTF-8 bytes so any
// JS string is encodable.
function utf8Btoa(str) {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function utf8Atob(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

export function encodeFramedTitle(frameJson, originalTitle = "") {
  if (typeof frameJson !== "string") {
    throw new TypeError("frameJson must be a string");
  }
  const encoded = utf8Btoa(frameJson);
  return originalTitle ? `${originalTitle} [holo:1:${encoded}]` : `[holo:1:${encoded}]`;
}

export function decodeFramedTitle(title) {
  if (typeof title !== "string") return null;
  const m = title.match(FRAMED_RE);
  if (!m) return null;
  try {
    return utf8Atob(m[1]);
  } catch {
    return null;
  }
}

export function encodePlainMarker(marker, originalTitle = "") {
  if (typeof marker !== "string") {
    throw new TypeError("marker must be a string");
  }
  if (marker.includes("]")) {
    throw new Error("marker must not contain ']'");
  }
  if (marker.startsWith("1:")) {
    // Reserved for the framed form; refuse to emit a plain marker that
    // would round-trip as framed.
    throw new Error("marker must not start with '1:'");
  }
  return originalTitle ? `${originalTitle} [holo:${marker}]` : `[holo:${marker}]`;
}

export function decodePlainMarker(title) {
  if (typeof title !== "string") return null;
  const m = title.match(PLAIN_RE);
  if (!m) return null;
  // Refuse to interpret a framed payload as a plain marker.
  if (m[1].startsWith("1:")) return null;
  return m[1];
}

export function isHoloTitle(title) {
  return decodeFramedTitle(title) !== null || decodePlainMarker(title) !== null;
}
