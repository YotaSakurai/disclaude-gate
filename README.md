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
  │                                     │  🔧 Bash
  │     (waiting for response...)       │  rm -rf /tmp/old
  │                                     │
  │                                     │  [✅ Allow] [❌ Deny] [💬 Reply]
  │                                     │
  │   ← decision returned ─────────────┤  *tap*
  │   ↓                                 │
  ├─ Tool executes (or is denied)       │
```

## How It Works

1. Claude Code fires a **PreToolUse hook** before executing any tool (Bash, Edit, Write, etc.)
2. The hook script sends the tool details to a **local HTTP server**
3. The server forwards it to your **Discord channel** with interactive buttons
4. You tap **Allow**, **Deny**, or **Reply** (with a custom message) on your phone
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
   - Bot Permissions: `Send Messages`, `Use Slash Commands`
6. Open the generated URL to invite the bot to your server
7. Create a dedicated channel (e.g. `#claude-approvals`) and copy its ID
   - Enable Developer Mode in Discord settings → right-click channel → Copy Channel ID

### 2. Install disclaude-gate

```bash
git clone https://github.com/your-username/disclaude-gate.git
cd disclaude-gate
./install.sh
```

The installer will:
- Install Python dependencies
- Create a `.env` file for your configuration
- Register the hook in Claude Code's `~/.claude/settings.json`

### 3. Configure

Edit `.env`:

```env
DISCORD_TOKEN=your-bot-token-here
DISCORD_CHANNEL_ID=123456789012345678
```

### 4. Run

```bash
# Start the server (keep running in background)
disclaude-gate

# In another terminal, use Claude Code as normal
claude
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_TOKEN` | (required) | Discord bot token |
| `DISCORD_CHANNEL_ID` | (required) | Channel ID for approval messages |
| `APPROVAL_TIMEOUT` | `300` | Seconds to wait before auto-deny |
| `PORT` | `19280` | Local HTTP server port |

### Auto-Allow List

By default, read-only tools (`Read`, `Glob`, `Grep`, etc.) are auto-allowed without sending a Discord notification. Edit the `AUTO_ALLOW_TOOLS` set in `hooks/disclaude_gate_hook.py` to customize:

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

### Allow / Deny
Tap the button. Claude Code proceeds or stops accordingly.

### Reply
Tap **Reply** → type a message → Claude reads it and adjusts its approach.

Example replies:
- "Don't delete that file, make a backup first"
- "Use pip instead of npm"
- "Skip this step and move on to testing"

### Graceful Fallback
If the server is not running, the hook silently falls through and Claude Code shows its normal terminal prompt. You can always approve locally.

## Running as a Service

### systemd (Linux)

```bash
cat > ~/.config/systemd/user/disclaude-gate.service << EOF
[Unit]
Description=disclaude-gate server
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/disclaude-gate
ExecStart=/usr/bin/python3 -m src.server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user enable --now disclaude-gate
```

## Architecture

```
┌─────────────────────────────────────────┐
│ Claude CLI                              │
│   └─ PreToolUse Hook                    │
│       └─ hooks/disclaude_gate_hook.py   │
│           │                             │
│           │ HTTP POST /approve          │
│           ▼                             │
│ src/server.py                           │
│   ├─ aiohttp (HTTP server, port 19280) │
│   └─ discord.py (Bot)                  │
│           │                             │
│           │ Discord API                 │
│           ▼                             │
│ Discord Channel                         │
│   └─ Embed + Buttons (Allow/Deny/Reply) │
└─────────────────────────────────────────┘
```

## License

MIT
