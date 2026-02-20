#!/usr/bin/env python3
"""Claude Code Stop hook — sends completion notification to disclaude-gate server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

SERVER_URL = "http://127.0.0.1:19280"


def _get_tmux_pane() -> str:
    """Get the current tmux pane ID, or empty string if not in tmux."""
    if not os.environ.get("TMUX"):
        return ""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def main() -> None:
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, Exception):
        return

    payload = json.dumps({
        "session_id": hook_input.get("session_id", ""),
        "transcript_path": hook_input.get("transcript_path", ""),
        "cwd": hook_input.get("cwd", ""),
        "stop_reason": hook_input.get("stop_reason", ""),
        "tmux_pane": _get_tmux_pane(),
    }).encode()

    req = urllib.request.Request(
        f"{SERVER_URL}/notify-stop",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    main()
