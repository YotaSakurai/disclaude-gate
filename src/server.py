"""disclaude-gate: Local HTTP server + Discord bot for remote Claude Code approval."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
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

def _parse_channel_id(raw: str) -> int:
    """Accept a numeric ID or a Discord channel URL."""
    raw = raw.strip().rstrip("/")
    # https://discord.com/channels/SERVER_ID/CHANNEL_ID
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    return int(raw) if raw else 0

DISCORD_CHANNEL_ID: int = _parse_channel_id(os.environ.get("DISCORD_CHANNEL_ID", "0"))
APPROVAL_TIMEOUT: int = int(os.environ.get("APPROVAL_TIMEOUT", "300"))
PORT: int = int(os.environ.get("PORT", "19280"))

# ---------------------------------------------------------------------------
# Session context helpers
# ---------------------------------------------------------------------------

def _extract_session_context(transcript_path: str, cwd: str) -> tuple[str, str]:
    """Extract session title and recent conversation context from transcript.

    Returns (session_title, recent_context).
    """
    session_title = ""
    recent_context = ""

    # Try to get session title from sessions-index.json
    if transcript_path:
        tp = Path(transcript_path)
        project_dir = tp.parent
        index_path = project_dir / "sessions-index.json"
        session_id = tp.stem  # filename without .jsonl
        if index_path.is_file():
            try:
                index_data = json.loads(index_path.read_text())
                for session in index_data:
                    if session.get("sessionId") == session_id:
                        session_title = session.get("summary", "")[:100]
                        break
            except Exception:
                pass

    # Fallback title: use working directory name
    if not session_title and cwd:
        session_title = Path(cwd).name

    # Extract recent user messages from transcript
    if transcript_path and Path(transcript_path).is_file():
        try:
            lines = Path(transcript_path).read_text().splitlines()
            # Read last N lines to find recent user messages
            user_messages: list[str] = []
            for line in reversed(lines[-50:]):
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, Exception):
                    continue
                # Claude Code transcript format: entry.type == "human", content in entry.message.content[]
                if entry.get("type") == "human":
                    msg = entry.get("message", {})
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                        content = " ".join(texts)
                    if isinstance(content, str) and content.strip():
                        # Skip system/hook messages
                        if not content.startswith("{") and not content.startswith("<") and len(content) < 500:
                            user_messages.append(content.strip())
                            if len(user_messages) >= 2:
                                break

            if user_messages:
                # Most recent first, reverse to chronological
                user_messages.reverse()
                recent_context = "\n".join(f"> {msg[:200]}" for msg in user_messages)
        except Exception:
            pass

    return session_title, recent_context


def _extract_last_assistant_message(transcript_path: str) -> str:
    """Extract the last assistant text message from the transcript."""
    if not transcript_path or not Path(transcript_path).is_file():
        return ""
    try:
        lines = Path(transcript_path).read_text().splitlines()
        for line in reversed(lines[-100:]):
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, Exception):
                continue
            # Transcript format: entry.type == "assistant", content in entry.message.content[]
            if entry.get("type") == "assistant":
                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", [])
                if isinstance(content, list):
                    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    result = "\n".join(t for t in texts if t.strip())
                    if result.strip():
                        return result.strip()
    except Exception:
        pass
    return ""

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

# Sessions that have had at least one approval request go through
_sessions_with_approvals: set[str] = set()

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


def _tmux_send_keys(tmux_pane: str, text: str) -> bool:
    """Send text to a tmux pane as keyboard input."""
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_pane, text, "Enter"],
            capture_output=True, timeout=5, check=True,
        )
        return True
    except Exception:
        return False


class StopReplyModal(ui.Modal, title="Reply to Claude"):
    """Modal for typing a reply to a stopped Claude session via tmux."""

    message = ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Yes, go ahead / Use approach A",
        max_length=1000,
    )

    def __init__(self, tmux_pane: str) -> None:
        super().__init__()
        self.tmux_pane = tmux_pane

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = str(self.message).strip()
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, _tmux_send_keys, self.tmux_pane, text)
        if success:
            embed = discord.Embed(description=f"Sent: {text}", color=discord.Color.blue())
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Failed to send to tmux pane.", ephemeral=True)


class StopView(ui.View):
    """Discord button: Reply to a stopped Claude session."""

    def __init__(self, tmux_pane: str) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.tmux_pane = tmux_pane

    @ui.button(label="Reply", style=discord.ButtonStyle.primary, emoji="\U0001f4ac")
    async def reply(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.send_modal(StopReplyModal(self.tmux_pane))


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

    @ui.button(label="Allow", style=discord.ButtonStyle.success, emoji="\u2705")
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

    @ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="\u274c")
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

    @ui.button(label="Reply", style=discord.ButtonStyle.primary, emoji="\U0001f4ac")
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
    transcript_path: str = body.get("transcript_path", "")
    cwd: str = body.get("cwd", "")

    session_id: str = body.get("session_id", "")

    if not request_id:
        return web.json_response({"error": "request_id required"}, status=400)

    # Track sessions that have gone through approval
    if session_id:
        _sessions_with_approvals.add(session_id)

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

    # Extract session context (run in executor to avoid blocking)
    loop = asyncio.get_running_loop()
    session_title, recent_context = await loop.run_in_executor(
        None, _extract_session_context, transcript_path, cwd
    )

    # Format the tool input for display
    input_display = _format_tool_input(tool_name, tool_input)

    embed = discord.Embed(
        title=f"\U0001f527 {tool_name}",
        description=input_display,
        color=discord.Color.gold(),
    )

    # Add session info
    if session_title:
        embed.set_author(name=session_title)

    # Add recent conversation context
    if recent_context:
        embed.add_field(name="Recent conversation", value=_truncate(recent_context, 1000), inline=False)

    footer_parts = [f"ID: {request_id[:8]}\u2026", f"Timeout: {APPROVAL_TIMEOUT}s"]
    if cwd:
        footer_parts.append(Path(cwd).name)
    embed.set_footer(text=" | ".join(footer_parts))

    view = ApprovalView(request_id)
    await channel.send(embed=embed, view=view)
    log.info("Approval request sent to Discord: %s [%s] session=%s", tool_name, request_id[:8], session_title or "?")

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


async def handle_stop(request: web.Request) -> web.Response:
    """Receive a stop notification — Claude session has finished."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    session_id: str = body.get("session_id", "")
    transcript_path: str = body.get("transcript_path", "")
    cwd: str = body.get("cwd", "")
    stop_reason: str = body.get("stop_reason", "")
    tmux_pane: str = body.get("tmux_pane", "")

    # Only notify for sessions that had approval requests
    if session_id not in _sessions_with_approvals:
        log.info("Stop ignored (no approvals): session=%s", session_id[:8] if session_id else "?")
        return web.json_response({"status": "skipped", "reason": "no approvals in session"})

    # Don't clean up yet — session may continue after user replies
    # _sessions_with_approvals will be cleaned up on next stop without reply

    await _bot_ready.wait()

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)
        except Exception:
            return web.json_response({"error": "Discord channel not found"}, status=500)

    loop = asyncio.get_running_loop()
    session_title, _ = await loop.run_in_executor(
        None, _extract_session_context, transcript_path, cwd
    )
    last_message = await loop.run_in_executor(
        None, _extract_last_assistant_message, transcript_path
    )

    embed = discord.Embed(
        title="\u2705 Session finished",
        description=_truncate(last_message, 2000) if last_message else "No output captured.",
        color=discord.Color.green(),
    )

    if session_title:
        embed.set_author(name=session_title)

    if stop_reason:
        embed.add_field(name="Reason", value=stop_reason, inline=True)
    if cwd:
        embed.set_footer(text=Path(cwd).name)

    # If tmux pane is available, add Reply button
    if tmux_pane:
        view = StopView(tmux_pane)
        await channel.send(embed=embed, view=view)
        log.info("Stop notification sent to Discord (with reply): session=%s tmux=%s", session_title or "?", tmux_pane)
    else:
        await channel.send(embed=embed)
        log.info("Stop notification sent to Discord: session=%s", session_title or "?")
        # No tmux = no way to reply, so clean up tracking
        _sessions_with_approvals.discard(session_id)

    return web.json_response({"status": "ok"})


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok", "bot_ready": _bot_ready.is_set()})


def _format_tool_input(tool_name: str, tool_input: dict) -> str:
    """Format tool input for readable Discord display."""
    if tool_name == "Bash" and "command" in tool_input:
        cmd = tool_input["command"]
        desc = tool_input.get("description", "")
        parts = []
        if desc:
            parts.append(f"**{desc}**")
        parts.append(f"```bash\n{_truncate(cmd, 1500)}\n```")
        return "\n".join(parts)

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
    return text[:max_len] + "\n\u2026 (truncated)"

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
    app.router.add_post("/notify-stop", handle_stop)
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
