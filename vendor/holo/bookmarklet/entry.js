// Bookmarklet entry point. Bundled into the `javascript:` URL.
//
// The single side effect of clicking the bookmark: install the in-page
// agent. install() is idempotent — clicking twice on the same page
// doesn't double-install.

import { install } from "./core.js";

install();
