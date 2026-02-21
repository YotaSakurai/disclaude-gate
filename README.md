# disclaude-gate

Remote approval for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) via Discord.

Approve, deny, or send custom replies to Claude Code's permission requests — right from your phone.

```
Claude CLI (WSL/Linux/macOS)          Discord (Phone)
  │                                     │
  ├─ Bash: rm -rf /tmp/old              │
  │   ↓ PreToolUse Hook fires           │
  │   ↓                                 │
  │   → HTTP POST to local server ──────┤
  │                                     │  [disclaude-gate] 🔧 Bash
  │     (waiting for response...)       │  rm -rf /tmp/old
  │                                     │
  │                                     │  [✅ Allow] [❌ Deny] [💬 Reply] [🔓 Allow All]
  │                                     │
  │   ← decision returned ─────────────┤  *tap*
  │   ↓                                 │
  ├─ Tool executes (or is denied)       │
```

## Features

- **Auto-allow all operations** — Bash, Edit, Write, etc. are auto-approved without notification
- **AskUserQuestion support** — multiple-choice questions shown as tappable Discord buttons
- **Stop notifications** — get notified when Claude finishes or asks a question
  - Question detected (ends with `？`): **Yes / No / Reply** buttons
  - Paused (no question): **Reply** button only
  - Session finished (no tmux): informational notification
- **Session threads** — each Claude session gets its own Discord thread for clean separation
- **Session colors** — each session has a unique embed color for visual distinction
- **Agent Teams support** — shows which agent role (researcher, tester, etc.) is requesting
- **tmux integration** — reply to Claude's questions from Discord via tmux key injection
- **Graceful fallback** — if the server is down, Claude Code falls back to its normal terminal prompt

## How It Works

1. **PreToolUse hook** — auto-allows all tool calls (`{"decision": "allow"}`), except `AskUserQuestion` which is forwarded to Discord
2. **Stop hook** — fires when Claude's turn ends, sends a Discord notification with the last output
3. Notifications are sent to a **Discord thread** per session with interactive buttons
4. You tap a button or type a reply on your phone
5. The response is injected into the terminal via **tmux send-keys**

### Notification Flow

```
Tool call (Bash, Edit, Write, etc.)
  → Hook auto-allows → Claude proceeds (no notification)

AskUserQuestion
  → Discord notification with option buttons → user selects → injected via tmux

Claude stops (turn ends)
  → Last message ends with ？ → Discord: ❓ Yes/No/Reply buttons
  → Otherwise                → Discord: ⏸️ Reply button only
  → No tmux                  → Discord: ✅ informational only
```

## Prerequisites

- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed
- A Discord account and server
- **tmux** (recommended — required for remote reply from Discord)

## Setup

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it (e.g. "Claude Gate")
3. Go to **Bot** tab → click **Reset Token** → copy the token
4. Enable **Message Content Intent** under Privileged Gateway Intents
5. Go to **OAuth2** → **URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Create Public Threads`, `Send Messages in Threads`
6. Open the generated URL to invite the bot to your server
7. Create a dedicated channel (e.g. `#claude-approvals`) and copy its ID
   - Enable Developer Mode in Discord settings → right-click channel → Copy Channel ID

### 2. Install disclaude-gate

```bash
git clone https://github.com/YotaSakurai/disclaude-gate.git
cd disclaude-gate
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
DISCORD_TOKEN=your-bot-token-here
DISCORD_CHANNEL_ID=123456789012345678
```

You can also paste a full channel URL — the ID will be extracted automatically.

### 4. Register Hooks

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/disclaude-gate/.venv/bin/python3 /path/to/disclaude-gate/hooks/disclaude_gate_hook.py"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/disclaude-gate/.venv/bin/python3 /path/to/disclaude-gate/hooks/disclaude_gate_stop_hook.py"
          }
        ]
      }
    ]
  }
}
```

Or run `./install.sh` to set this up automatically.

### 5. tmux Setup (recommended)

disclaude-gate can inject responses into Claude's terminal via tmux. Add to `~/.bashrc`:

```bash
# Auto-start tmux on interactive terminal
if [ -z "$TMUX" ] && [ -n "$PS1" ] && command -v tmux &>/dev/null; then
    exec tmux new-session
fi
```

This ensures every terminal tab runs inside tmux, enabling Discord-to-terminal reply.

### 6. CLAUDE.md Rule (recommended)

Add to your global `~/CLAUDE.md`:

```markdown
- ユーザーの判断や返答が必要な場合、メッセージの最後を必ず「？」で終えてください（Discord経由の通知検出に使われます）
```

This ensures Claude always ends with `？` when it needs user input, making question detection reliable.

### 7. Run

```bash
# Start the server
.venv/bin/disclaude-gate

# In another terminal, use Claude Code as normal
claude
```

## Running as a Service (recommended)

Run disclaude-gate as a systemd user service so it starts automatically:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/disclaude-gate.service << EOF
[Unit]
Description=disclaude-gate server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/disclaude-gate
ExecStart=/path/to/disclaude-gate/.venv/bin/python3 -m src.server
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user enable --now disclaude-gate
```

Check status: `systemctl --user status disclaude-gate`

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_TOKEN` | (required) | Discord bot token |
| `DISCORD_CHANNEL_ID` | (required) | Channel ID or URL for the approval channel |
| `APPROVAL_TIMEOUT` | `300` | Seconds to wait before auto-deny |
| `PORT` | `19280` | Local HTTP server port |

## Usage

### AskUserQuestion
When Claude asks a multiple-choice question, each option appears as a tappable button. Tap **Other** for free-text input.

### Stop Notifications

| Claude's last message | Title | Buttons |
|---|---|---|
| Ends with `？` or `?` | ❓ Waiting for input | Yes / No / Reply |
| Anything else (tmux) | ⏸️ Paused | Reply |
| No tmux available | ✅ Session finished | None |

### Session Threads
Each Claude session automatically gets its own Discord thread, keeping conversations organized. Threads are named after the session and auto-archive after 1 hour of inactivity.

### Agent Teams
When using Claude Code's Agent Teams feature, the agent's role name is displayed in the notification title (e.g. `🤖 researcher › 🔧 Bash`), so you know which team member is requesting approval.

### Graceful Fallback
If the server is not running, the hook silently falls through and Claude Code shows its normal terminal prompt.

## Architecture

```
┌──────────────────────────────────────────────────┐
│ Claude CLI                                       │
│   ├─ PreToolUse Hook → hooks/disclaude_gate_hook.py
│   │   └─ Auto-allows all tools (except AskUserQuestion)
│   └─ Stop Hook       → hooks/disclaude_gate_stop_hook.py
│          │                                       │
│          │ HTTP POST                             │
│          ▼                                       │
│ src/server.py                                    │
│   ├─ aiohttp (HTTP server, port 19280)           │
│   └─ discord.py (Bot)                            │
│          │                                       │
│          │ Discord API                           │
│          ▼                                       │
│ Discord Channel                                  │
│   ├─ Thread: "my-project"                        │
│   │   ├─ ❓ Waiting for input [Yes] [No] [Reply] │
│   │   └─ ⏸️ Paused [Reply]                       │
│   └─ Thread: "agent-team"                        │
│       ├─ 🤖 researcher › ❓ Question             │
│       └─ ✅ Session finished                     │
└──────────────────────────────────────────────────┘
```

## License

MIT
