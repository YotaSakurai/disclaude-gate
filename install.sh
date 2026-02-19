#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_PATH="$SCRIPT_DIR/hooks/disclaude_gate_hook.py"
SETTINGS_FILE="$HOME/.claude/settings.json"

echo "=== disclaude-gate installer ==="
echo ""

# 1. Install Python dependencies
echo "[1/3] Installing Python dependencies..."
if command -v pip3 &>/dev/null; then
    pip3 install -e "$SCRIPT_DIR"
elif command -v pip &>/dev/null; then
    pip install -e "$SCRIPT_DIR"
else
    echo "ERROR: pip not found. Please install Python 3.10+ and pip."
    exit 1
fi
echo "    Done."
echo ""

# 2. Set up .env if not present
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "[2/3] Setting up configuration..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "    Created .env from template."
    echo "    Please edit $SCRIPT_DIR/.env with your Discord bot token and channel ID."
    echo ""
    read -p "    Open .env in editor now? [Y/n] " -r
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        "${EDITOR:-nano}" "$SCRIPT_DIR/.env"
    fi
else
    echo "[2/3] .env already exists, skipping."
fi
echo ""

# 3. Register hook in Claude Code settings
echo "[3/3] Registering Claude Code hook..."

mkdir -p "$(dirname "$SETTINGS_FILE")"

if [ ! -f "$SETTINGS_FILE" ]; then
    # Create settings file with the hook
    cat > "$SETTINGS_FILE" << EOJSON
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 $HOOK_PATH"
          }
        ]
      }
    ]
  }
}
EOJSON
    echo "    Created $SETTINGS_FILE with hook."
else
    # Check if hook is already registered
    if grep -q "disclaude_gate_hook" "$SETTINGS_FILE" 2>/dev/null; then
        echo "    Hook already registered in settings.json."
    else
        echo "    settings.json already exists."
        echo "    Please add the following hook manually:"
        echo ""
        echo '    "hooks": {'
        echo '      "PreToolUse": ['
        echo '        {'
        echo '          "matcher": "",'
        echo '          "hooks": ['
        echo '            {'
        echo '              "type": "command",'
        echo "              \"command\": \"python3 $HOOK_PATH\""
        echo '            }'
        echo '          ]'
        echo '        }'
        echo '      ]'
        echo '    }'
        echo ""
        echo "    Or run: python3 $SCRIPT_DIR/scripts/register_hook.py"
    fi
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Make sure .env has your DISCORD_TOKEN and DISCORD_CHANNEL_ID"
echo "  2. Start the server:  disclaude-gate"
echo "  3. Start Claude Code in another terminal — approvals will appear in Discord!"
echo ""
