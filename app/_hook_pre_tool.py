#!/usr/bin/env python3
"""PreToolUse hook script for VibeNode.

Called by Claude Code CLI before each tool use. Reads hook event from stdin,
POSTs it to the GUI server, and blocks until the user approves/denies.
Outputs decision JSON to stdout. Exit 0 = proceed, exit 2 = block.
"""

import sys
import json
import urllib.request
import urllib.error

SERVER = "http://localhost:5050"

def main():
    # Read hook event from stdin
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except Exception:
        # Can't parse — allow by default
        print(json.dumps({"decision": "allow"}))
        sys.exit(0)

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    session_id = event.get("session_id", "")

    # POST to GUI server and wait for response
    payload = json.dumps({
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{SERVER}/api/hook/pre-tool",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError:
        # Server unreachable — allow by default
        print(json.dumps({"decision": "allow"}))
        sys.exit(0)
    except Exception:
        print(json.dumps({"decision": "allow"}))
        sys.exit(0)

    action = result.get("action", "allow")
    if action == "deny":
        # Exit 2 = block the tool use
        reason = result.get("reason", "Denied by user")
        print(json.dumps({"decision": "block", "reason": reason}))
        sys.exit(2)
    else:
        print(json.dumps({"decision": "allow"}))
        sys.exit(0)

if __name__ == "__main__":
    main()
