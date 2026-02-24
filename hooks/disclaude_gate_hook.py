#!/usr/bin/env python3
"""Claude Code PreToolUse hook — auto-allows all operations except AskUserQuestion
and unrecoverable destructive Bash commands.

AskUserQuestion is forwarded to disclaude-gate so the user can answer from Discord.
Destructive Bash commands (rm, rmdir, etc.) are forwarded for Discord approval ONLY
when the targets are NOT tracked by git (i.e. not recoverable from git history).
Everything else is auto-approved without notification.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

SERVER_URL = "http://127.0.0.1:19280"
DEBUG_LOG = "/tmp/disclaude-hook-debug.log"

# Commands that delete or destroy data
DESTRUCTIVE_COMMANDS = {
    "rm", "rmdir", "shred", "unlink",
}

# Destructive git subcommands — these destroy uncommitted/untracked work
# which is NOT recoverable from git history, so always require approval
DESTRUCTIVE_GIT_PATTERNS = [
    r"\bgit\s+clean\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+push\s+.*--force\b",
    r"\bgit\s+push\s+.*-f\b",
    r"\bgit\s+branch\s+.*-[dD]\b",
    r"\bgit\s+checkout\s+--\s",       # git checkout -- <file> (discard uncommitted)
    r"\bgit\s+restore\s+(?!--staged)", # git restore <file> (discard uncommitted)
]


def _log(msg: str) -> None:
    """Append debug line to log file."""
    try:
        import datetime
        with open(DEBUG_LOG, "a") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S} {msg}\n")
    except Exception:
        pass


def _parse_rm_targets(part: str) -> list[str]:
    """Extract file/dir targets from an rm/rmdir/shred/unlink command string."""
    try:
        tokens = shlex.split(part)
    except ValueError:
        return []  # Can't parse → empty triggers approval

    # Skip env vars and sudo to find the command name
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if "=" in tok and not tok.startswith("-"):
            idx += 1
            continue
        if tok == "sudo":
            idx += 1
            continue
        break

    # Skip command name itself (rm, rmdir, etc.)
    idx += 1

    # Collect non-flag arguments as file paths
    paths: list[str] = []
    past_separator = False
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--" and not past_separator:
            past_separator = True
            idx += 1
            continue
        if not past_separator and tok.startswith("-"):
            idx += 1
            continue
        paths.append(tok)
        idx += 1
    return paths


def _all_git_recoverable(paths: list[str]) -> bool:
    """Check if ALL deletion targets are recoverable from git history.

    Returns True only when every target is tracked by git and there is
    no untracked content that would be lost.  Returns False (= needs
    Discord approval) when in doubt.
    """
    # Must be inside a git repo
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return False
    except Exception:
        return False

    # Get repo root to detect paths outside the repo
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        repo_root = proc.stdout.strip() if proc.returncode == 0 else None
    except Exception:
        repo_root = None

    if not paths:
        return False  # No targets found → can't verify, be safe

    for path in paths:
        # Paths with shell expansion ($, `, subshells) can't be verified
        if any(c in path for c in ("$", "`", "(", ")")):
            return False

        # Absolute paths outside the repo are not recoverable
        if os.path.isabs(path) and repo_root:
            resolved = os.path.normpath(path)
            if not resolved.startswith(repo_root + os.sep) and resolved != repo_root:
                return False

        try:
            # Check for untracked files matching this path (includes .gitignore'd files)
            proc = subprocess.run(
                ["git", "ls-files", "--others", path],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return False  # Would delete untracked content

            # Verify the path has some tracked content
            proc = subprocess.run(
                ["git", "ls-files", path],
                capture_output=True, text=True, timeout=5,
            )
            # If nothing tracked and nothing untracked → path doesn't exist, rm is a no-op → safe
            # If tracked content exists → recoverable from git history → safe
        except Exception:
            return False

    return True


def _needs_discord_approval_bash(command: str) -> bool:
    """Check if a Bash command needs Discord approval.

    Destructive file commands (rm, etc.) are approved automatically when
    all targets are tracked by git.  Only unrecoverable deletions and
    destructive git operations require Discord approval.
    """
    # Split compound commands on ||, &&, ;, |, newlines
    parts = re.split(r"\|\||&&|;|\||\n", command)
    for part in parts:
        stripped = part.strip()
        if not stripped:
            continue

        # Git destructive patterns always need approval (destroy uncommitted work)
        for pattern in DESTRUCTIVE_GIT_PATTERNS:
            if re.search(pattern, stripped):
                return True

        # Extract the base command name (skip env vars and sudo)
        tokens = stripped.split()
        idx = 0
        while idx < len(tokens):
            tok = tokens[idx]
            if "=" in tok and not tok.startswith("-"):
                idx += 1
                continue
            if tok == "sudo":
                idx += 1
                continue
            break
        if idx >= len(tokens):
            continue
        base_cmd = os.path.basename(tokens[idx])

        if base_cmd in DESTRUCTIVE_COMMANDS:
            targets = _parse_rm_targets(stripped)
            if not _all_git_recoverable(targets):
                return True
            # All targets git-tracked → recoverable, no approval needed

    return False


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

    # Check if this is a destructive Bash command that needs Discord approval
    needs_discord = False
    if tool_name == "AskUserQuestion":
        needs_discord = True
    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if _needs_discord_approval_bash(command):
            _log(f"DESTRUCTIVE_BASH: {command}")
            needs_discord = True

    # Non-destructive operations are auto-allowed
    if not needs_discord:
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
