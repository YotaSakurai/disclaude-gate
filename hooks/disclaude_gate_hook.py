#!/usr/bin/env python3
"""Claude Code PreToolUse hook — sends approval requests to disclaude-gate server."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SERVER_URL = "http://127.0.0.1:19280"
DEBUG_LOG = "/tmp/disclaude-hook-debug.log"

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
    "TaskCreate",
    "TaskUpdate",
    "Task",
    "SendMessage",
    "TeamCreate",
    "TeamDelete",
    "TaskStop",
    "TaskOutput",
    "EnterPlanMode",
    "ExitPlanMode",
    "Skill",
    "NotebookEdit",
    "Edit",
    "Write",
    "EnterWorktree",
}


def _load_claude_permissions() -> list[str]:
    """Load the allow list from Claude Code's settings.local.json."""
    settings_path = Path.home() / ".claude" / "settings.local.json"
    try:
        if settings_path.is_file():
            data = json.loads(settings_path.read_text())
            return data.get("permissions", {}).get("allow", [])
    except Exception:
        pass
    return []


def _is_bash_allowed_by_claude(command: str, allow_list: list[str]) -> bool:
    """Check if a Bash command matches any pattern in Claude's permission allow list.

    Patterns look like:
      "Bash(git commit:*)"  — matches commands starting with "git commit"
      "Bash(ls:*)"          — matches commands starting with "ls"
      "Bash(exact command)"  — matches the exact command
    """
    for entry in allow_list:
        # Only check Bash(...) entries
        m = re.match(r'^Bash\((.+)\)$', entry)
        if not m:
            continue
        pattern = m.group(1)
        # "git commit:*" → match commands starting with "git commit"
        if pattern.endswith(":*"):
            prefix = pattern[:-2]
            if command == prefix or command.startswith(prefix + " ") or command.startswith(prefix + "\n"):
                return True
        else:
            # Exact match
            if command == pattern:
                return True
    return False


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
        # If we can't parse, let Claude Code handle it normally
        _log("PARSE_ERROR: couldn't read stdin")
        return

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    session_id = hook_input.get("session_id", "")[:8]
    permission_mode = hook_input.get("permission_mode", "")

    _log(f"START tool={tool_name} session={session_id} mode={permission_mode}")

    # Auto-allow safe tools
    if tool_name in AUTO_ALLOW_TOOLS:
        _log(f"AUTO_ALLOW tool={tool_name}")
        return

    # Auto-allow Bash commands that Claude Code already permits
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        allow_list = _load_claude_permissions()
        if _is_bash_allowed_by_claude(command, allow_list):
            _log(f"AUTO_ALLOW_BASH command={command[:80]}")
            return

    # AskUserQuestion without tmux: can't inject answer remotely,
    # so let Claude Code handle it in the terminal directly
    if tool_name == "AskUserQuestion" and not os.environ.get("TMUX"):
        _log(f"SKIP_ASK: no tmux, falling through to terminal")
        return

    request_id = str(uuid.uuid4())

    # Forward all hook fields plus our request_id and tmux pane
    payload_dict = dict(hook_input)
    payload_dict["request_id"] = request_id
    # Capture tmux pane for Agent Teams identification
    if os.environ.get("TMUX"):
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
    # else: no output → falls through to normal Claude Code prompt


if __name__ == "__main__":
    main()
