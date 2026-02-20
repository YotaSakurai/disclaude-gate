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

- **Allow / Deny / Reply** — approve, reject, or send custom instructions from your phone
- **Allow All** — auto-approve all remaining requests in a session (great for Agent Teams)
- **AskUserQuestion support** — multiple-choice questions shown as tappable Discord buttons
- **Session threads** — each Claude session gets its own Discord thread for clean separation
- **Session colors** — each session has a unique embed color for visual distinction
- **Agent Teams support** — shows which agent role (researcher, tester, etc.) is requesting
- **Completion notifications** — get notified when a session finishes, with the last output
- **tmux reply** — reply to Claude's questions from Discord when running in tmux
- **Graceful fallback** — if the server is down, Claude Code falls back to its normal terminal prompt
- **Auto-allow list** — read-only tools (Read, Glob, Grep, etc.) skip Discord entirely

## How It Works

1. Claude Code fires a **PreToolUse hook** before executing any tool (Bash, Edit, Write, etc.)
2. The hook script sends the tool details to a **local HTTP server**
3. The server creates a **Discord thread** for the session and posts an embed with interactive buttons
4. You tap a button or type a reply on your phone
5. The decision is returned to Claude Code, which proceeds accordingly

**Key difference from full-remote solutions:** disclaude-gate only notifies you when approval is needed. Your normal CLI workflow stays untouched.

## Prerequisites

- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed
- A Discord account and server

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

### 5. Run

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

### Auto-Allow List

By default, read-only tools are auto-allowed without sending a Discord notification. Edit `AUTO_ALLOW_TOOLS` in `hooks/disclaude_gate_hook.py` to customize:

```python
AUTO_ALLOW_TOOLS = {
    "Read",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "TaskList",
    "TaskGet",
}
```

## Usage

### Allow / Deny / Reply
Tap **Allow** or **Deny**. Tap **Reply** to type a custom message — Claude reads it and adjusts its approach.

### Allow All
Tap **Allow All** to auto-approve all remaining requests in that session. Useful when running Agent Teams with many parallel agents.

### AskUserQuestion
When Claude asks a multiple-choice question, each option appears as a tappable button. Tap **Other** for free-text input.

### Session Threads
Each Claude session automatically gets its own Discord thread, keeping conversations organized. Threads are named after the session and auto-archive after 1 hour of inactivity.

### Agent Teams
When using Claude Code's Agent Teams feature, the agent's role name is displayed in the notification title (e.g. `🤖 researcher › 🔧 Bash`), so you know which team member is requesting approval.

### Completion Notifications
When a session that went through approval finishes, you get a notification with Claude's last output — no need to keep checking the terminal.

### tmux Reply (optional)
Run Claude in tmux to enable replying to Claude's questions from Discord:

```bash
tmux new -s work
claude
```

When Claude stops and waits for input, the completion notification includes a **Reply** button that sends your response directly to the terminal.

### Graceful Fallback
If the server is not running, the hook silently falls through and Claude Code shows its normal terminal prompt.

## Architecture

```
┌──────────────────────────────────────────────────┐
│ Claude CLI                                       │
│   ├─ PreToolUse Hook → hooks/disclaude_gate_hook.py
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
│   ├─ Thread: "disclaude-gate"                    │
│   │   ├─ 🔧 Bash [Allow] [Deny] [Reply] [Allow All]
│   │   └─ ✅ Session finished                     │
│   └─ Thread: "predict-horse"                     │
│       ├─ 🤖 researcher › 🔧 Bash                │
│       └─ ✅ Session finished                     │
└──────────────────────────────────────────────────┘
```

## License

MIT
