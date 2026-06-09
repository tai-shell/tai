// Build script — bundles the ES modules into a single IIFE, wraps it
// as a `javascript:` URL, and emits a static install page.
//
// Output:
//   dist/bookmarklet.js    minified IIFE (the bookmark's payload, unwrapped)
//   dist/install.html      static page with a draggable "🔧 holo" link
//
// Usage: `npm run build` (from the bookmarklet/ directory).

import { build } from "esbuild";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const distDir = path.join(here, "dist");

const bundle = await build({
  entryPoints: [path.join(here, "entry.js")],
  bundle: true,
  format: "iife",
  minify: true,
  target: ["chrome100", "firefox100", "safari16"],
  write: false,
  legalComments: "none",
});

if (bundle.errors.length > 0) {
  console.error("bundle errors:", bundle.errors);
  process.exit(1);
}

const code = bundle.outputFiles[0].text;
const urlEncoded = encodeURIComponent(code);
const bookmarklet = `javascript:${urlEncoded}`;

const html = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Install holo</title>
    <style>
      :root { color-scheme: light dark; }
      body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        max-width: 38em; margin: 4em auto; padding: 0 1.25em;
        line-height: 1.55; color: #222; background: #fafafa;
      }
      h1 { font-weight: 600; margin-bottom: 0.25em; }
      .lede { color: #555; margin-top: 0; }
      a.bookmarklet {
        display: inline-block;
        padding: 0.5em 1em;
        background: rgba(0, 200, 0, 0.18);
        border: 1px solid rgba(0, 100, 0, 0.45);
        border-radius: 0.4em;
        color: #114;
        text-decoration: none;
        font-weight: 600;
        cursor: grab;
        user-select: none;
      }
      a.bookmarklet:active { cursor: grabbing; }
      ol { padding-left: 1.25em; }
      li { margin: 0.4em 0; }
      code, kbd { background: #ececec; padding: 0.1em 0.35em; border-radius: 0.25em; font-size: 0.9em; }
      footer { margin-top: 3em; color: #888; font-size: 0.85em; }
      @media (prefers-color-scheme: dark) {
        body { color: #ddd; background: #1a1a1a; }
        .lede { color: #aaa; }
        a.bookmarklet { background: rgba(0, 220, 0, 0.22); border-color: rgba(0, 200, 0, 0.5); color: #ddf; }
        code, kbd { background: #2a2a2a; }
        footer { color: #666; }
      }
    </style>
  </head>
  <body>
    <h1>Install <code>holo</code></h1>
    <p class="lede">In-page agent for the holo browser-automation primitive.</p>

    <ol>
      <li>Show the bookmarks bar in your browser if it's hidden.</li>
      <li>Drag the link below onto the bookmarks bar.</li>
      <li>Open any normal <code>http(s)</code> page and click the bookmark to install the agent.</li>
    </ol>

    <p><a class="bookmarklet" href="${bookmarklet}">🔧 holo</a></p>

    <p>The first click also acts as a calibration handshake — start <code>holo daemon</code> on the same machine, then click the bookmark; the daemon detects the calibration beacon and locks onto the active browser window.</p>

    <footer>
      <p>Apache-2.0 · <a href="https://github.com/nospaceleftondevice/holo">github.com/nospaceleftondevice/holo</a></p>
    </footer>
  </body>
</html>
`;

await fs.mkdir(distDir, { recursive: true });
await fs.writeFile(path.join(distDir, "bookmarklet.js"), code);
await fs.writeFile(path.join(distDir, "install.html"), html);

const sizeBytes = Buffer.byteLength(code, "utf8");
const urlBytes = Buffer.byteLength(bookmarklet, "utf8");
console.log(`bookmarklet: ${sizeBytes} bytes minified (${urlBytes} bytes URL-encoded)`);
console.log(`wrote ${path.relative(process.cwd(), path.join(distDir, "bookmarklet.js"))}`);
console.log(`wrote ${path.relative(process.cwd(), path.join(distDir, "install.html"))}`);
