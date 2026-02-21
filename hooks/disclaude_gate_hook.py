#!/usr/bin/env python3
"""Claude Code PreToolUse hook — auto-allows all operations except AskUserQuestion.

AskUserQuestion is forwarded to disclaude-gate so the user can answer from Discord.
Everything else (Bash, Edit, Write, etc.) is auto-approved without notification.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

SERVER_URL = "http://127.0.0.1:19280"
DEBUG_LOG = "/tmp/disclaude-hook-debug.log"


def _log(msg: str) -> None:
    """Append debug line to log file."""
    try:
        import datetime
        with open(DEBUG_LOG, "a") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S} {msg}\n")
    except Exception:
        pass


def main() -> None:
    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, Exception):
        _log("PARSE_ERROR: couldn't read stdin")
        return

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    session_id = hook_input.get("session_id", "")[:8]

    _log(f"START tool={tool_name} session={session_id}")

    # Only AskUserQuestion needs Discord interaction — everything else is auto-allowed.
    if tool_name != "AskUserQuestion":
        _log(f"AUTO_ALLOW tool={tool_name}")
        print(json.dumps({"decision": "allow"}))
        return

    # AskUserQuestion without tmux: can't inject answer remotely,
    # so let Claude Code handle it in the terminal directly.
    if not os.environ.get("TMUX"):
        _log("SKIP_ASK: no tmux, falling through to terminal")
        return

    # Forward AskUserQuestion to disclaude-gate server for Discord interaction.
    request_id = str(uuid.uuid4())
    payload_dict = dict(hook_input)
    payload_dict["request_id"] = request_id

    # Capture tmux pane for remote answer injection
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            payload_dict["tmux_pane"] = proc.stdout.strip()
    except Exception:
        pass

    payload = json.dumps(payload_dict).encode()

    req = urllib.request.Request(
        f"{SERVER_URL}/approve",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    _log(f"SENDING to server tool={tool_name}")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw_resp = resp.read()
            result = json.loads(raw_resp)
    except urllib.error.URLError as e:
        # Server not running — fall through to normal CLI prompt
        _log(f"URL_ERROR: {e}")
        print(json.dumps({"error": "disclaude-gate server not reachable"}), file=sys.stderr)
        return
    except Exception as e:
        _log(f"EXCEPTION: {e}")
        return

    decision = result.get("decision")
    reason = result.get("reason")
    _log(f"RESPONSE tool={tool_name} decision={decision} reason={reason}")

    if decision == "allow":
        output_json = json.dumps({"decision": "allow"})
        _log(f"OUTPUT: {output_json}")
        print(output_json)
    elif decision == "deny":
        output: dict = {"decision": "deny"}
        if reason:
            output["reason"] = reason
        output_json = json.dumps(output)
        _log(f"OUTPUT: {output_json}")
        print(output_json)
    else:
        _log(f"NO_OUTPUT: decision was {decision!r} — falling through")


if __name__ == "__main__":
    main()
