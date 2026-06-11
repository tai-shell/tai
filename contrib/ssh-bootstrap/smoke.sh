#!/bin/bash
# smoke.sh — end-to-end agentless verification.
#
# Drives mcp-bridge.py the same way Claude will, with a canned
# JSON-RPC sequence. mcp-bridge.py does the full auto-bootstrap
# itself — no separate bootstrap.sh invocation needed.
#
# Sends: initialize → notifications/initialized → tools/list →
# tools/call (doctor) → tools/call (screen_move 500 500). Looks
# for the tools/list count and for the screen_move response.
#
# Usage:
#     TAI_BINARY=/path/to/tai-bundled-macos-universal2 \
#         ./smoke.sh user@hostB [PORT]

set -u    # no `-e`: we WANT the script to keep going after a bridge
          # failure so the stderr log + diagnostics get printed
HERE="$(cd "$(dirname "$0")" && pwd)"

TARGET=${1:-}
PORT=${2:-7081}
if [ -z "$TARGET" ]; then
    echo "Usage: $0 user@hostB [PORT]" >&2
    exit 64
fi

BRIDGE="$HERE/mcp-bridge.py"
if [ ! -x "$BRIDGE" ]; then
    echo "ERROR: $BRIDGE not executable. chmod +x it." >&2
    exit 2
fi

OUT=$(mktemp -t tai-smoke.XXXXXX)
trap 'rm -f "$OUT"' EXIT

# Build the JSON-RPC stream. Each line is one frame.
{
    printf '%s\n' \
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}'
    printf '%s\n' \
        '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    printf '%s\n' \
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    printf '%s\n' \
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"doctor"}}'
    printf '%s\n' \
        '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"screen_move","arguments":{"x":500,"y":500}}}'
    # Let the server finish processing the move before we drop stdin
    # and tear down — otherwise the process exits before screen_move
    # has finished.
    sleep 3
} | "$BRIDGE" "$TARGET" "$PORT" > "$OUT" 2> /tmp/tai-smoke.stderr.txt
BRIDGE_EXIT=${PIPESTATUS[1]}

echo ""
echo "=== bridge stderr ==="
cat /tmp/tai-smoke.stderr.txt
echo ""
echo "=== bridge exit code: $BRIDGE_EXIT ==="
if [ "$BRIDGE_EXIT" -ne 0 ]; then
    echo ""
    echo "(bridge exited non-zero; results section will likely show NO RESPONSE)"
    echo ""
fi

# Parse the results
python3 - <<PY
import json, sys
results_by_id = {}
with open("$OUT") as f:
    for line in f:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        if "id" in m:
            results_by_id[m["id"]] = m

def show(rid, label):
    r = results_by_id.get(rid)
    if r is None:
        print(f"  ✗ {label}: NO RESPONSE for id={rid}")
        return False
    if "error" in r:
        print(f"  ✗ {label}: error {r['error']}")
        return False
    print(f"  ✓ {label}")
    return r

print("=== smoke results ===")
init = show(1, "initialize")
tools = show(2, "tools/list")
if tools:
    n = len(tools["result"]["tools"])
    print(f"      → {n} tools registered")
doctor = show(3, "doctor")
move = show(4, "screen_move 500 500")
print()
ok = all([init, tools, doctor, move])
print("OVERALL:", "✓ PASS" if ok else "✗ FAIL")
sys.exit(0 if ok else 1)
PY
