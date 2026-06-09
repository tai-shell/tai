# holo bridge — SikuliX Jython service

The bridge is a Jython 2.7 script that runs inside SikuliX's bundled
JVM. The Python daemon talks to it over JSON-RPC (stdio by default;
TCP optional for remote / cross-host driving). All UI interaction —
window activation, mouse clicks, keystrokes, image find, OCR — is
delegated to SikuliX through this bridge.

## Why Jython, not Java?

The user-facing dependency story is "install OpenJDK + drop a single
`holo` binary". No Gradle, no Maven, no compiled wrapper jar. The
"java code" is just `bridge.py`, executed by `sikulixapi.jar -r`.

## Provisioning the SikuliX jar

Not committed to the repo — large (75 MB for the API jar, 123 MB for
the IDE jar) and third-party. Three ways to provide it, in the order
the bridge looks:

1. **PyInstaller bundle (release builds).** The release workflow
   downloads `sikulixapi-2.0.5.jar` from a pinned GitHub Release
   asset and includes it in the onefile binary at build time. End
   users get one self-contained download.
2. **`vendor/` in the repo (dev).** Drop `sikulixapi-*.jar` (or
   `sikulixide-*.jar` if that's what you have) into `vendor/` at
   the repo root. The `.gitignore` keeps it out of git.
3. **User cache (auto-download).** `holo install-bridge` (planned)
   downloads the jar from the same GitHub Release into
   `~/Library/Caches/holo` (macOS) or `~/.cache/holo` (Linux), so
   slim binary releases can fetch it on first run.

Either jar works — the resolver prefers `sikulixapi-*.jar` (smaller)
when both are present.

Override the search entirely with `HOLO_SIKULI_JAR=/path/to/jar` or
`BridgeClient(jar_path=...)`.

Pinned version: **SikuliX 2.0.5** (Jython 2.7.2 inside).

## Running locally

```bash
# stdio (the daemon will normally do this for you)
java -jar vendor/sikulixapi.jar -r bridge/bridge.py

# stdio, explicit
java -jar vendor/sikulixapi.jar -r bridge/bridge.py -- --transport stdio

# TCP (remote driving)
java -jar vendor/sikulixapi.jar -r bridge/bridge.py -- \
    --transport tcp --bind 127.0.0.1 --port 7081 --token mysecret
```

The `--` after the script path separates SikuliX's args from the
bridge's own args.

## Protocol

Line-delimited JSON-RPC. One request = one line; one response = one
line. Newlines in payloads are escaped by `json.dumps`, so the
framing is safe.

Request:

```json
{"id": "abc", "method": "screen.click", "params": {"x": 100, "y": 200}}
```

Success response:

```json
{"id": "abc", "result": {"clicked": true, "x": 100, "y": 200}}
```

Error response:

```json
{"id": "abc", "error": {"code": -32603, "message": "...", "trace": "..."}}
```

For TCP, the first line of every connection is a handshake:

```json
{"token": "mysecret"}
```

Server replies with `{"ok": true, "protocol": "1"}` on success or
`{"ok": false, "error": "bad token"}` and closes on failure.

## Methods (initial set)

| Method          | Params                                               | Result                                |
| ---             | ---                                                  | ---                                   |
| `ping`          | —                                                    | `{pong, protocol, sikuli}`            |
| `app.activate`  | `{name}`                                             | `{focused, name}`                     |
| `screen.click`  | `{x, y, modifiers?: ["cmd", ...]}`                   | `{clicked, x, y}`                     |
| `screen.key`    | `{combo}` — e.g. `"cmd+v"`, `"enter"`, `"shift+tab"` | `{sent}`                              |
| `screen.type`   | `{text}`                                             | `{typed_chars}`                       |

More (`screen.shot`, `screen.find_image`, `screen.find_text`) land in
a follow-up PR.

## Security note for TCP

Always bind to `127.0.0.1` unless you know what you're doing. Anyone
reachable on the bound interface can drive the host's screen. The
token check is the only authentication, and the channel isn't
encrypted — for off-loopback use, tunnel through SSH or a VPN.
