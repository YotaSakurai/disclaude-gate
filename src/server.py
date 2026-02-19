"""disclaude-gate: Local HTTP server + Discord bot for remote Claude Code approval."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import discord
from aiohttp import web
from discord import ui

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("disclaude-gate")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env file if present (minimal implementation, no extra dependency)."""
    for env_path in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if value and key:
                    os.environ.setdefault(key, value)
            break


_load_env()

DISCORD_TOKEN: str = os.environ.get("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID: int = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
APPROVAL_TIMEOUT: int = int(os.environ.get("APPROVAL_TIMEOUT", "300"))
PORT: int = int(os.environ.get("PORT", "19280"))

# ---------------------------------------------------------------------------
# Pending request store
# ---------------------------------------------------------------------------

@dataclass
class PendingRequest:
    request_id: str
    tool_name: str
    tool_input: dict
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: str | None = None       # "allow" | "deny"
    reason: str | None = None


# request_id -> PendingRequest
_pending: dict[str, PendingRequest] = {}

# ---------------------------------------------------------------------------
# Discord UI components
# ---------------------------------------------------------------------------

class ReplyModal(ui.Modal, title="Reply to Claude"):
    """Modal for typing a custom reply message."""

    message = ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Don't delete that file — read it first.",
        max_length=1000,
    )

    def __init__(self, request_id: str) -> None:
        super().__init__()
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        req = _pending.get(self.request_id)
        if req is None:
            await interaction.response.send_message("This request has already expired.", ephemeral=True)
            return
        req.decision = "deny"
        req.reason = str(self.message)
        req.event.set()
        embed = discord.Embed(
            description=f"Replied: {self.message}",
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)


class ApprovalView(ui.View):
    """Discord buttons: Allow / Deny / Reply."""

    def __init__(self, request_id: str) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.request_id = request_id

    async def on_timeout(self) -> None:
        req = _pending.get(self.request_id)
        if req and not req.event.is_set():
            req.decision = "deny"
            req.reason = "Timed out waiting for approval"
            req.event.set()

    @ui.button(label="Allow", style=discord.ButtonStyle.success, emoji="✅")
    async def allow(self, interaction: discord.Interaction, button: ui.Button) -> None:
        req = _pending.get(self.request_id)
        if req is None:
            await interaction.response.send_message("This request has already expired.", ephemeral=True)
            return
        req.decision = "allow"
        req.event.set()
        embed = discord.Embed(description="Allowed", color=discord.Color.green())
        await interaction.response.send_message(embed=embed)
        self.stop()

    @ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: ui.Button) -> None:
        req = _pending.get(self.request_id)
        if req is None:
            await interaction.response.send_message("This request has already expired.", ephemeral=True)
            return
        req.decision = "deny"
        req.event.set()
        embed = discord.Embed(description="Denied", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        self.stop()

    @ui.button(label="Reply", style=discord.ButtonStyle.primary, emoji="💬")
    async def reply(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.send_modal(ReplyModal(self.request_id))

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = discord.Client(intents=intents)

_bot_ready = asyncio.Event()


@bot.event
async def on_ready() -> None:
    log.info("Discord bot connected as %s", bot.user)
    _bot_ready.set()

# ---------------------------------------------------------------------------
# HTTP API (called by the hook script)
# ---------------------------------------------------------------------------

async def handle_approval(request: web.Request) -> web.Response:
    """Receive a tool approval request from the hook script."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    request_id: str = body.get("request_id", "")
    tool_name: str = body.get("tool_name", "unknown")
    tool_input: dict = body.get("tool_input", {})

    if not request_id:
        return web.json_response({"error": "request_id required"}, status=400)

    # Create pending request
    req = PendingRequest(request_id=request_id, tool_name=tool_name, tool_input=tool_input)
    _pending[request_id] = req

    # Wait for bot to be ready
    await _bot_ready.wait()

    # Send Discord message
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)
        except Exception:
            _pending.pop(request_id, None)
            return web.json_response({"error": "Discord channel not found"}, status=500)

    # Format the tool input for display
    input_display = _format_tool_input(tool_name, tool_input)

    embed = discord.Embed(
        title=f"🔧 {tool_name}",
        description=input_display,
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"ID: {request_id[:8]}… | Timeout: {APPROVAL_TIMEOUT}s")

    view = ApprovalView(request_id)
    await channel.send(embed=embed, view=view)
    log.info("Approval request sent to Discord: %s [%s]", tool_name, request_id[:8])

    # Wait for user response
    try:
        await asyncio.wait_for(req.event.wait(), timeout=APPROVAL_TIMEOUT + 5)
    except asyncio.TimeoutError:
        req.decision = "deny"
        req.reason = "Timed out waiting for approval"

    _pending.pop(request_id, None)

    result: dict = {"decision": req.decision or "deny"}
    if req.reason:
        result["reason"] = req.reason

    log.info("Returning decision: %s (reason=%s)", result["decision"], result.get("reason"))
    return web.json_response(result)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok", "bot_ready": _bot_ready.is_set()})


def _format_tool_input(tool_name: str, tool_input: dict) -> str:
    """Format tool input for readable Discord display."""
    if tool_name == "Bash" and "command" in tool_input:
        cmd = tool_input["command"]
        return f"```bash\n{_truncate(cmd, 1500)}\n```"

    if tool_name == "Write" and "file_path" in tool_input:
        content = tool_input.get("content", "")
        path = tool_input["file_path"]
        preview = _truncate(content, 800)
        return f"**File:** `{path}`\n```\n{preview}\n```"

    if tool_name == "Edit" and "file_path" in tool_input:
        path = tool_input["file_path"]
        old = _truncate(tool_input.get("old_string", ""), 400)
        new = _truncate(tool_input.get("new_string", ""), 400)
        return f"**File:** `{path}`\n**Old:**\n```\n{old}\n```\n**New:**\n```\n{new}\n```"

    if tool_name == "Read" and "file_path" in tool_input:
        return f"**File:** `{tool_input['file_path']}`"

    # Generic fallback
    formatted = json.dumps(tool_input, ensure_ascii=False, indent=2)
    return f"```json\n{_truncate(formatted, 1500)}\n```"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n… (truncated)"

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def _run_http(app: web.Application) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    log.info("HTTP server listening on http://127.0.0.1:%d", PORT)


async def _async_main() -> None:
    # Validate config
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN is not set. See .env.example")
        sys.exit(1)
    if not DISCORD_CHANNEL_ID:
        log.error("DISCORD_CHANNEL_ID is not set. See .env.example")
        sys.exit(1)

    # HTTP server
    app = web.Application()
    app.router.add_post("/approve", handle_approval)
    app.router.add_get("/health", handle_health)

    await _run_http(app)

    # Discord bot (runs forever)
    try:
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        pass
    finally:
        await bot.close()


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
