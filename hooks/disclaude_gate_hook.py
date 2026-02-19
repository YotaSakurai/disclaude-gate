#!/usr/bin/env python3
"""Claude Code PreToolUse hook — sends approval requests to disclaude-gate server."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid

SERVER_URL = "http://127.0.0.1:19280"

# Tools that are generally safe and don't need approval.
# Customize this list to your preference.
AUTO_ALLOW_TOOLS = {
    "Read",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "TaskList",
    "TaskGet",
}


def main() -> None:
    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, Exception):
        # If we can't parse, let Claude Code handle it normally
        return

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    # Auto-allow safe tools
    if tool_name in AUTO_ALLOW_TOOLS:
        return

    request_id = str(uuid.uuid4())

    payload = json.dumps({
        "request_id": request_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": hook_input.get("session_id", ""),
        "transcript_path": hook_input.get("transcript_path", ""),
        "cwd": hook_input.get("cwd", ""),
    }).encode()

    req = urllib.request.Request(
        f"{SERVER_URL}/approve",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError:
        # Server not running — fall through to normal CLI prompt
        print(json.dumps({"error": "disclaude-gate server not reachable"}), file=sys.stderr)
        return
    except Exception:
        return

    decision = result.get("decision")
    reason = result.get("reason")

    if decision == "allow":
        print(json.dumps({"decision": "allow"}))
    elif decision == "deny":
        output: dict = {"decision": "deny"}
        if reason:
            output["reason"] = reason
        print(json.dumps(output))
    # else: no output → falls through to normal Claude Code prompt


if __name__ == "__main__":
    main()
