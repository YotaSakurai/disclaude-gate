"""disclaude-gate: Local HTTP server + Discord bot for remote Claude Code approval."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
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
# Session color helper
# ---------------------------------------------------------------------------

def _session_color(session_id: str) -> discord.Color:
    """Generate a stable, visually distinct color from a session ID."""
    if not session_id:
        return discord.Color.gold()
    h = hash(session_id) & 0xFFFFFF
    # Boost saturation by keeping values away from grey
    r = ((h >> 16) & 0xFF) | 0x40
    g = ((h >> 8) & 0xFF) | 0x40
    b = (h & 0xFF) | 0x40
    return discord.Color.from_rgb(r & 0xFF, g & 0xFF, b & 0xFF)

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

    # Fallback title: find git repo root name, or use a meaningful cwd segment
    if not session_title and cwd:
        cwd_path = Path(cwd)
        # Walk up to find .git directory (repo root)
        for parent in [cwd_path, *cwd_path.parents]:
            if (parent / ".git").exists():
                session_title = parent.name
                break
            if parent == parent.parent:
                break
        if not session_title:
            session_title = cwd_path.name

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


def _extract_agent_name(transcript_path: str, tmux_pane: str = "") -> str:
    """Extract agent role name for Agent Teams and Task subagents.

    Strategy:
    1. Match tmux pane ID against team config members (tmux-based Agent Teams)
    2. For Task subagents, read description from parent transcript's queue-operation
    """
    teams_dir = Path.home() / ".claude" / "teams"

    # Strategy 1: Match tmux pane against team config
    if tmux_pane and teams_dir.is_dir():
        for team_dir in teams_dir.iterdir():
            config_path = team_dir / "config.json"
            if not config_path.is_file():
                continue
            try:
                config = json.loads(config_path.read_text())
                for member in config.get("members", []):
                    if member.get("tmuxPaneId") == tmux_pane:
                        name = member.get("name", "")
                        # Skip team-lead (it's the main session)
                        if name and name != "team-lead":
                            return name
            except Exception:
                continue

    if not transcript_path:
        return ""
    tp = Path(transcript_path)

    # Strategy 2: Task subagent — read description from parent transcript
    if tp.parent.name == "subagents":
        agent_id = tp.stem.replace("agent-", "")
        if agent_id and not agent_id.startswith("compact"):
            # Parent transcript: {session_id}.jsonl in grandparent directory
            parent_transcript = tp.parent.parent.parent / f"{tp.parent.parent.name}.jsonl"
            if parent_transcript.is_file():
                needle = f'"task_id":"{agent_id}"'
                try:
                    with open(parent_transcript) as f:
                        for line in f:
                            if needle not in line:
                                continue
                            entry = json.loads(line)
                            content = entry.get("content", "")
                            if isinstance(content, str) and "description" in content:
                                inner = json.loads(content)
                                desc = inner.get("description", "")
                                if desc:
                                    return desc
                            break  # found the task entry, stop searching
                except Exception:
                    pass

    return ""


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
# Thread management — one Discord thread per session
# ---------------------------------------------------------------------------

# session_id -> discord.Thread
_session_threads: dict[str, discord.Thread] = {}


async def _find_existing_thread(
    channel: discord.TextChannel, thread_name: str,
) -> discord.Thread | None:
    """Search for an existing (possibly archived) thread by name."""
    # Check active threads
    for thread in channel.threads:
        if thread.name == thread_name:
            if thread.archived:
                await thread.edit(archived=False)
            return thread

    # Check archived threads
    async for thread in channel.archived_threads(limit=50):
        if thread.name == thread_name:
            await thread.edit(archived=False)
            return thread

    return None


async def _get_or_create_thread(
    channel: discord.TextChannel, session_id: str, session_title: str,
) -> discord.Thread:
    """Get an existing thread for the session or create a new one."""
    if session_id in _session_threads:
        thread = _session_threads[session_id]
        try:
            if not thread.archived:
                return thread
            # Unarchive if needed
            await thread.edit(archived=False)
            return thread
        except Exception:
            pass

    thread_name = session_title or session_id[:12]
    # Discord thread name limit is 100 chars
    if len(thread_name) > 100:
        thread_name = thread_name[:97] + "..."

    # Try to reuse existing thread with same name
    thread = await _find_existing_thread(channel, thread_name)
    if thread:
        _session_threads[session_id] = thread
        log.info("Reusing thread '%s' for session %s", thread_name, session_id[:8])
        return thread

    thread = await channel.create_thread(
        name=thread_name,
        type=discord.ChannelType.public_thread,
        auto_archive_duration=60,
    )
    _session_threads[session_id] = thread
    log.info("Created thread '%s' for session %s", thread_name, session_id[:8])
    return thread


async def _archive_thread(session_id: str) -> None:
    """Archive the thread for a completed session."""
    thread = _session_threads.pop(session_id, None)
    if thread:
        try:
            await thread.edit(archived=True)
            log.info("Archived thread for session %s", session_id[:8])
        except Exception:
            pass

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

# Sessions where user has chosen "Allow All" — auto-approve everything
_auto_allow_sessions: set[str] = set()

# ---------------------------------------------------------------------------
# Discord UI components
# ---------------------------------------------------------------------------

async def _mark_resolved(
    msg: discord.Message,
    view: ui.View,
    status: str,
    color: discord.Color,
    detail: str = "",
) -> None:
    """Update the original approval message to show it's been resolved."""
    # Update embed: prepend status, change color, add detail
    embed = msg.embeds[0] if msg.embeds else discord.Embed()
    embed.title = f"{status} {embed.title or ''}"
    embed.color = color
    if detail:
        embed.add_field(name="Response", value=detail, inline=False)
    # Disable all buttons
    for item in view.children:
        item.disabled = True
    try:
        await msg.edit(embed=embed, view=view)
        log.debug("_mark_resolved: updated message (status=%s)", status)
    except Exception:
        log.exception("_mark_resolved: failed to update message")


class ReplyModal(ui.Modal, title="Reply to Claude"):
    """Modal for typing a custom reply message."""

    message = ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Don't delete that file — read it first.",
        max_length=1000,
    )

    def __init__(self, request_id: str, original_message: discord.Message | None = None,
                 parent_view: ui.View | None = None) -> None:
        super().__init__()
        self.request_id = request_id
        self.original_message = original_message
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        req = _pending.get(self.request_id)
        if req is None:
            await interaction.response.send_message("This request has already expired.", ephemeral=True)
            return
        req.decision = "deny"
        req.reason = str(self.message)
        req.event.set()
        await interaction.response.send_message(
            embed=discord.Embed(description=f"Replied: {self.message}", color=discord.Color.blue()),
        )
        if self.original_message and self.parent_view:
            await _mark_resolved(self.original_message, self.parent_view,
                                 "\U0001f4ac", discord.Color.blue(), str(self.message)[:200])


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


def _tmux_select_option(tmux_pane: str, option_index: int) -> bool:
    """Select option at given index in an interactive CLI prompt via tmux arrow keys."""
    try:
        for _ in range(option_index):
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_pane, "Down"],
                capture_output=True, timeout=5, check=True,
            )
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_pane, "Enter"],
            capture_output=True, timeout=5, check=True,
        )
        return True
    except Exception:
        return False


def _tmux_select_multi_options(tmux_pane: str, option_indices: list[int]) -> bool:
    """Toggle multiple options in a multi-select prompt and submit via tmux."""
    try:
        current_pos = 0
        for idx in sorted(option_indices):
            moves = idx - current_pos
            for _ in range(moves):
                subprocess.run(
                    ["tmux", "send-keys", "-t", tmux_pane, "Down"],
                    capture_output=True, timeout=5, check=True,
                )
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_pane, "Space"],
                capture_output=True, timeout=5, check=True,
            )
            current_pos = idx
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_pane, "Enter"],
            capture_output=True, timeout=5, check=True,
        )
        return True
    except Exception:
        return False


def _tmux_type_other_option(tmux_pane: str, text: str, num_options: int) -> bool:
    """Select 'Other' option and type custom text via tmux."""
    try:
        # Navigate past all defined options to reach 'Other'
        for _ in range(num_options):
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_pane, "Down"],
                capture_output=True, timeout=5, check=True,
            )
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_pane, "Enter"],
            capture_output=True, timeout=5, check=True,
        )
        time.sleep(0.2)  # Wait for text input prompt
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
    """Discord buttons for replying to a stopped Claude session."""

    def __init__(self, tmux_pane: str) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.tmux_pane = tmux_pane

    async def _send_and_resolve(self, interaction: discord.Interaction, text: str) -> None:
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, _tmux_send_keys, self.tmux_pane, text)
        if success:
            await interaction.response.defer()
            await _mark_resolved(interaction.message, self, "\U0001f4ac", discord.Color.blue(), text)
        else:
            await interaction.response.send_message("Failed to send to tmux pane.", ephemeral=True)
        self.stop()

    @ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await self._send_and_resolve(interaction, "yes")

    @ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await self._send_and_resolve(interaction, "no")

    @ui.button(label="Reply", style=discord.ButtonStyle.primary, emoji="\U0001f4ac")
    async def reply(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.send_modal(StopReplyModal(self.tmux_pane))


class AskUserQuestionView(ui.View):
    """Handles AskUserQuestion — single-question uses buttons, multi-question uses dropdowns."""

    def __init__(self, request_id: str, questions: list[dict]) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.request_id = request_id
        self._original_message: discord.Message | None = None
        self._answers: dict[int, str] = {}  # question index -> selected label(s)
        self._question_count = len(questions)

        if len(questions) <= 1:
            # Single question — use buttons (original behavior)
            options = questions[0].get("options", []) if questions else []
            for i, opt in enumerate(options[:20]):
                label = opt.get("label", f"Option {i + 1}")
                if len(label) > 80:
                    label = label[:77] + "..."
                button = ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary,
                    custom_id=f"ask_{request_id[:8]}_{i}",
                )
                button.callback = self._make_button_callback(label)
                self.add_item(button)
        else:
            # Multi-question — use Select menus (dropdowns)
            # Discord allows max 5 ActionRows, each with 1 Select or up to 5 buttons
            for qi, q in enumerate(questions[:4]):
                options = q.get("options", [])
                is_multi = q.get("multiSelect", False)
                header = q.get("header", f"Q{qi + 1}")
                select_options = []
                for oi, opt in enumerate(options[:25]):
                    label = opt.get("label", f"Option {oi + 1}")
                    desc = opt.get("description", "")
                    if len(label) > 100:
                        label = label[:97] + "..."
                    if len(desc) > 100:
                        desc = desc[:97] + "..."
                    select_options.append(discord.SelectOption(
                        label=label,
                        description=desc or None,
                        value=label,
                    ))
                select = ui.Select(
                    placeholder=f"{header}: {q.get('question', '')[:90]}",
                    options=select_options,
                    min_values=1,
                    max_values=len(select_options) if is_multi else 1,
                    custom_id=f"ask_{request_id[:8]}_q{qi}",
                )
                select.callback = self._make_select_callback(qi)
                self.add_item(select)
            # Submit button in the last row
            submit_btn = ui.Button(
                label="Submit",
                style=discord.ButtonStyle.success,
                emoji="\u2705",
                custom_id=f"ask_{request_id[:8]}_submit",
            )
            submit_btn.callback = self._submit_callback
            self.add_item(submit_btn)

        # Free-text reply button (always present)
        reply_btn = ui.Button(
            label="Other",
            style=discord.ButtonStyle.secondary,
            emoji="\U0001f4ac",
            custom_id=f"ask_{request_id[:8]}_reply",
        )
        reply_btn.callback = self._reply_callback
        self.add_item(reply_btn)

    # -- Single question: button callbacks --

    def _make_button_callback(self, label: str):
        async def callback(interaction: discord.Interaction) -> None:
            req = _pending.get(self.request_id)
            if req is None:
                await interaction.response.send_message("This request has already expired.", ephemeral=True)
                return
            req.decision = "deny"
            req.reason = label
            req.event.set()
            await interaction.response.send_message(
                embed=discord.Embed(description=f"Selected: {label}", color=discord.Color.blue()),
            )
            await _mark_resolved(interaction.message, self, "\U0001f4ac", discord.Color.blue(), label)
            self.stop()
        return callback

    # -- Multi-question: select + submit callbacks --

    def _make_select_callback(self, question_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            values = interaction.data.get("values", [])
            self._answers[question_index] = ", ".join(values)
            answered = len(self._answers)
            await interaction.response.defer()
        return callback

    async def _submit_callback(self, interaction: discord.Interaction) -> None:
        req = _pending.get(self.request_id)
        if req is None:
            await interaction.response.send_message("This request has already expired.", ephemeral=True)
            return
        if len(self._answers) < self._question_count:
            unanswered = self._question_count - len(self._answers)
            await interaction.response.send_message(
                f"Please answer all questions ({unanswered} remaining).", ephemeral=True,
            )
            return
        # Format all answers as text
        parts = [f"Q{i + 1}: {self._answers[i]}" for i in sorted(self._answers)]
        combined = "\n".join(parts)
        req.decision = "deny"
        req.reason = combined
        req.event.set()
        await interaction.response.send_message(
            embed=discord.Embed(description=f"Submitted:\n{combined}", color=discord.Color.blue()),
        )
        await _mark_resolved(interaction.message, self, "\U0001f4ac", discord.Color.blue(),
                             combined[:200])
        self.stop()

    # -- Common --

    async def _reply_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ReplyModal(self.request_id, interaction.message, self),
        )

    async def on_timeout(self) -> None:
        req = _pending.get(self.request_id)
        if req and not req.event.is_set():
            req.decision = "deny"
            req.reason = "Timed out waiting for response"
            req.event.set()
            if self._original_message:
                await _mark_resolved(self._original_message, self, "\u23f0", discord.Color.dark_grey())


class AskQuestionTmuxReplyModal(ui.Modal, title="Reply to Claude"):
    """Modal for typing a custom answer that gets injected via tmux."""

    message = ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="Type your answer...",
        max_length=1000,
    )

    def __init__(self, tmux_pane: str, num_options: int,
                 original_message: discord.Message | None = None,
                 parent_view: ui.View | None = None) -> None:
        super().__init__()
        self.tmux_pane = tmux_pane
        self.num_options = num_options
        self.original_message = original_message
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = str(self.message).strip()
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(
            None, _tmux_type_other_option, self.tmux_pane, text, self.num_options
        )
        if success:
            await interaction.response.defer()
            if self.original_message and self.parent_view:
                await _mark_resolved(
                    self.original_message, self.parent_view,
                    "\U0001f4ac", discord.Color.blue(), text[:200]
                )
        else:
            await interaction.response.send_message("Failed to send to tmux.", ephemeral=True)


class AskQuestionTmuxView(ui.View):
    """AskUserQuestion view that injects answers into the terminal via tmux."""

    def __init__(self, tmux_pane: str, questions: list[dict]) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.tmux_pane = tmux_pane
        self._original_message: discord.Message | None = None
        self._questions = questions

        if len(questions) <= 1:
            # Single question — buttons for each option
            options = questions[0].get("options", []) if questions else []
            for i, opt in enumerate(options[:20]):
                label = opt.get("label", f"Option {i + 1}")
                if len(label) > 80:
                    label = label[:77] + "..."
                button = ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary,
                    custom_id=f"tmux_ask_{id(self)}_{i}",
                )
                button.callback = self._make_option_callback(i, label)
                self.add_item(button)
        else:
            # Multi-question — select menus + submit
            self._answer_indices: dict[int, list[int]] = {}
            self._answer_labels: dict[int, list[str]] = {}
            for qi, q in enumerate(questions[:4]):
                options = q.get("options", [])
                is_multi = q.get("multiSelect", False)
                header = q.get("header", f"Q{qi + 1}")
                select_options = []
                for oi, opt in enumerate(options[:25]):
                    label = opt.get("label", f"Option {oi + 1}")
                    desc = opt.get("description", "")
                    if len(label) > 100:
                        label = label[:97] + "..."
                    if len(desc) > 100:
                        desc = desc[:97] + "..."
                    select_options.append(discord.SelectOption(
                        label=label, description=desc or None, value=str(oi),
                    ))
                select = ui.Select(
                    placeholder=f"{header}: {q.get('question', '')[:90]}",
                    options=select_options,
                    min_values=1,
                    max_values=len(select_options) if is_multi else 1,
                    custom_id=f"tmux_ask_{id(self)}_q{qi}",
                )
                select.callback = self._make_select_callback(qi)
                self.add_item(select)
            submit_btn = ui.Button(
                label="Submit", style=discord.ButtonStyle.success, emoji="\u2705",
                custom_id=f"tmux_ask_{id(self)}_submit",
            )
            submit_btn.callback = self._submit_multi
            self.add_item(submit_btn)

        # "Other" button for free text
        other_btn = ui.Button(
            label="Other", style=discord.ButtonStyle.secondary, emoji="\U0001f4ac",
            custom_id=f"tmux_ask_{id(self)}_other",
        )
        other_btn.callback = self._other_callback
        self.add_item(other_btn)

    # -- Single question: button callbacks --

    def _make_option_callback(self, index: int, label: str):
        async def callback(interaction: discord.Interaction) -> None:
            loop = asyncio.get_running_loop()
            success = await loop.run_in_executor(
                None, _tmux_select_option, self.tmux_pane, index
            )
            if success:
                await interaction.response.defer()
                await _mark_resolved(
                    interaction.message, self, "\U0001f4ac", discord.Color.blue(), label
                )
            else:
                await interaction.response.send_message("Failed to send to tmux.", ephemeral=True)
            self.stop()
        return callback

    # -- Multi-question: select + submit callbacks --

    def _make_select_callback(self, qi: int):
        async def callback(interaction: discord.Interaction) -> None:
            values = interaction.data.get("values", [])
            self._answer_indices[qi] = [int(v) for v in values]
            options = self._questions[qi].get("options", [])
            self._answer_labels[qi] = [
                options[int(v)].get("label", f"Option {int(v)+1}")
                for v in values if int(v) < len(options)
            ]
            await interaction.response.defer()
        return callback

    async def _submit_multi(self, interaction: discord.Interaction) -> None:
        if len(self._answer_indices) < len(self._questions):
            remaining = len(self._questions) - len(self._answer_indices)
            await interaction.response.send_message(
                f"Please answer all questions ({remaining} remaining).", ephemeral=True,
            )
            return

        loop = asyncio.get_running_loop()
        for qi in range(len(self._questions)):
            indices = self._answer_indices.get(qi, [])
            is_multi = self._questions[qi].get("multiSelect", False)
            if is_multi:
                success = await loop.run_in_executor(
                    None, _tmux_select_multi_options, self.tmux_pane, indices
                )
            else:
                idx = indices[0] if indices else 0
                success = await loop.run_in_executor(
                    None, _tmux_select_option, self.tmux_pane, idx
                )
            if not success:
                await interaction.response.send_message("Failed to send to tmux.", ephemeral=True)
                return
            # Wait for next question prompt to appear
            if qi < len(self._questions) - 1:
                await asyncio.sleep(0.5)

        parts = [f"Q{qi+1}: {', '.join(self._answer_labels.get(qi, []))}"
                 for qi in range(len(self._questions))]
        summary = "\n".join(parts)
        await interaction.response.defer()
        await _mark_resolved(
            interaction.message, self, "\U0001f4ac", discord.Color.blue(), summary[:200]
        )
        self.stop()

    # -- Other (free text) --

    async def _other_callback(self, interaction: discord.Interaction) -> None:
        num_options = len(self._questions[0].get("options", [])) if self._questions else 0
        await interaction.response.send_modal(
            AskQuestionTmuxReplyModal(
                self.tmux_pane, num_options, interaction.message, self
            )
        )

    async def on_timeout(self) -> None:
        if self._original_message:
            await _mark_resolved(
                self._original_message, self, "\u23f0", discord.Color.dark_grey()
            )


class ApprovalView(ui.View):
    """Discord buttons: Allow / Deny / Reply / Allow All."""

    def __init__(self, request_id: str, session_id: str = "") -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.request_id = request_id
        self.session_id = session_id
        self._original_message: discord.Message | None = None

    async def on_timeout(self) -> None:
        req = _pending.get(self.request_id)
        if req and not req.event.is_set():
            req.decision = "deny"
            req.reason = "Timed out waiting for approval"
            req.event.set()
            if self._original_message:
                await _mark_resolved(self._original_message, self, "\u23f0", discord.Color.dark_grey())

    @ui.button(label="Allow", style=discord.ButtonStyle.success, emoji="\u2705")
    async def allow(self, interaction: discord.Interaction, button: ui.Button) -> None:
        req = _pending.get(self.request_id)
        if req is None:
            await interaction.response.send_message("This request has already expired.", ephemeral=True)
            return
        req.decision = "allow"
        req.event.set()
        await interaction.response.defer()
        await _mark_resolved(interaction.message, self, "\u2705", discord.Color.green())
        self.stop()

    @ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="\u274c")
    async def deny(self, interaction: discord.Interaction, button: ui.Button) -> None:
        req = _pending.get(self.request_id)
        if req is None:
            await interaction.response.send_message("This request has already expired.", ephemeral=True)
            return
        req.decision = "deny"
        req.event.set()
        await interaction.response.defer()
        await _mark_resolved(interaction.message, self, "\u274c", discord.Color.red())
        self.stop()

    @ui.button(label="Reply", style=discord.ButtonStyle.primary, emoji="\U0001f4ac")
    async def reply(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.send_modal(
            ReplyModal(self.request_id, interaction.message, self),
        )

    @ui.button(label="Allow All", style=discord.ButtonStyle.secondary, emoji="\U0001f513")
    async def allow_all(self, interaction: discord.Interaction, button: ui.Button) -> None:
        req = _pending.get(self.request_id)
        if req is None:
            await interaction.response.send_message("This request has already expired.", ephemeral=True)
            return
        log.info("Allow All tapped: session_id=%r, request_id=%s", self.session_id, self.request_id[:8])
        if self.session_id:
            _auto_allow_sessions.add(self.session_id)
            log.info("Session added to auto-allow: %s", self.session_id[:8])
        else:
            log.warning("Allow All: session_id is empty — auto-approve will NOT work")
        req.decision = "allow"
        req.event.set()
        await interaction.response.defer()
        await _mark_resolved(interaction.message, self, "\u2705", discord.Color.green(), "Auto-approving all")
        self.stop()

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

async def _ask_question_via_tmux(
    session_id: str, tool_name: str, tool_input: dict,
    transcript_path: str, cwd: str, tmux_pane: str,
) -> None:
    """Handle AskUserQuestion by sending Discord notification and injecting via tmux."""
    try:
        await _bot_ready.wait()

        channel = bot.get_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            log.error("AskUserQuestion tmux: Discord channel not found")
            return

        loop = asyncio.get_running_loop()
        session_title, recent_context = await loop.run_in_executor(
            None, _extract_session_context, transcript_path, cwd
        )
        agent_name = await loop.run_in_executor(
            None, _extract_agent_name, transcript_path, tmux_pane
        )

        questions = tool_input.get("questions", [])
        input_display = _format_tool_input(tool_name, tool_input)

        title_prefix = f"[{session_title}] " if session_title else ""
        agent_prefix = f"\U0001f916 {agent_name} \u203a " if agent_name else ""
        embed = discord.Embed(
            title=f"{title_prefix}{agent_prefix}\u2753 Question",
            description=input_display,
            color=_session_color(session_id),
        )
        if recent_context:
            embed.add_field(name="Recent conversation", value=_truncate(recent_context, 1000), inline=False)
        embed.set_footer(text=f"Timeout: {APPROVAL_TIMEOUT}s | tmux: {tmux_pane}")

        view = AskQuestionTmuxView(tmux_pane, questions)

        thread = await _get_or_create_thread(channel, session_id, session_title)
        sent_msg = await thread.send(embed=embed, view=view)
        view._original_message = sent_msg

        alert_title = session_title or "Unknown"
        agent_label = f" ({agent_name})" if agent_name else ""
        await channel.send(
            f"\U0001f514 **{alert_title}**{agent_label} asks: \u2753 Question \u2192 {thread.mention}"
        )
        log.info("AskUserQuestion sent to Discord (tmux mode): session=%s", session_title or "?")

    except Exception:
        log.exception("Failed to handle AskUserQuestion via tmux")


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

    # Auto-approve if "Allow All" was previously selected for this session
    if session_id in _auto_allow_sessions:
        log.info("Auto-approved (Allow All): %s [%s]", tool_name, session_id[:8])
        return web.json_response({"decision": "allow"})

    # AskUserQuestion + tmux: allow immediately, inject answer via tmux in background
    tmux_pane: str = body.get("tmux_pane", "")
    if tool_name == "AskUserQuestion" and tmux_pane:
        asyncio.create_task(_ask_question_via_tmux(
            session_id, tool_name, tool_input,
            transcript_path, cwd, tmux_pane,
        ))
        log.info("AskUserQuestion: allow + tmux inject (pane=%s, session=%s)",
                 tmux_pane, session_id[:8] if session_id else "?")
        return web.json_response({"decision": "allow"})

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
    agent_name = await loop.run_in_executor(
        None, _extract_agent_name, transcript_path, tmux_pane
    )
    if agent_name:
        log.info("Agent identified: %s (tmux=%s)", agent_name, tmux_pane or "n/a")

    # Format the tool input for display
    input_display = _format_tool_input(tool_name, tool_input)

    # Build title: [session] 🔧 Tool  or  [session] 🤖 agent > 🔧 Tool
    title_prefix = f"[{session_title}] " if session_title else ""
    agent_prefix = f"\U0001f916 {agent_name} \u203a " if agent_name else ""
    embed = discord.Embed(
        title=f"{title_prefix}{agent_prefix}\U0001f527 {tool_name}",
        description=input_display,
        color=_session_color(session_id),
    )

    # Add recent conversation context
    if recent_context:
        embed.add_field(name="Recent conversation", value=_truncate(recent_context, 1000), inline=False)

    footer_parts = [f"ID: {request_id[:8]}\u2026", f"Timeout: {APPROVAL_TIMEOUT}s"]
    if cwd:
        footer_parts.append(Path(cwd).name)
    embed.set_footer(text=" | ".join(footer_parts))

    # Use AskUserQuestion view if applicable
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        view = AskUserQuestionView(request_id, questions)
    else:
        view = ApprovalView(request_id, session_id)

    # Send to session thread
    thread = await _get_or_create_thread(channel, session_id, session_title)
    sent_msg = await thread.send(embed=embed, view=view)
    view._original_message = sent_msg

    # Post brief alert in main channel linking to the thread
    alert_title = session_title or "Unknown"
    agent_label = f" ({agent_name})" if agent_name else ""
    await channel.send(
        f"\U0001f514 **{alert_title}**{agent_label} needs approval: **{tool_name}** \u2192 {thread.mention}"
    )
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

    title_prefix = f"[{session_title}] " if session_title else ""
    if tmux_pane:
        # tmux available — Claude is waiting for input, show reply buttons
        title_icon = "\u2753"  # ❓ Waiting for input
        title_label = "Waiting for input"
    else:
        # No tmux — session finished, informational only
        title_icon = "\u2705"  # ✅ Session finished
        title_label = "Session finished"
    embed = discord.Embed(
        title=f"{title_prefix}{title_icon} {title_label}",
        description=_truncate(last_message, 2000) if last_message else "No output captured.",
        color=_session_color(session_id),
    )

    if stop_reason:
        embed.add_field(name="Reason", value=stop_reason, inline=True)
    if cwd:
        embed.set_footer(text=Path(cwd).name)

    # Send to session thread
    thread = await _get_or_create_thread(channel, session_id, session_title)

    if tmux_pane:
        # Always show Yes/No/Reply buttons when tmux is available
        view = StopView(tmux_pane)
        await thread.send(embed=embed, view=view)
        log.info("Stop notification sent (waiting): session=%s tmux=%s", session_title or "?", tmux_pane)
    else:
        await thread.send(embed=embed)
        log.info("Stop notification sent (finished): session=%s", session_title or "?")
        _sessions_with_approvals.discard(session_id)
        _auto_allow_sessions.discard(session_id)
        await _archive_thread(session_id)

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

    if tool_name == "AskUserQuestion" and "questions" in tool_input:
        questions = tool_input["questions"]
        parts = []
        for q in questions:
            parts.append(f"**{q.get('question', '')}**")
            for i, opt in enumerate(q.get("options", []), 1):
                label = opt.get("label", "")
                desc = opt.get("description", "")
                parts.append(f"{i}. **{label}**" + (f" — {desc}" if desc else ""))
        return "\n".join(parts)

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
