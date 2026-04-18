"""
Chat gating — who Clawy talks to, and when.

Three independent gates, all consulted before Clawy generates chat replies
(or proactive unsolicited replies):

  1. Quiet hours  — Clawy stays silent during a scheduled time window.
  2. Role allowlist — Clawy only chats with members of configured roles.
  3. (ollama health — existing, not here; handled in moderation.py)

None of these block moderation. Prefilter, blocklist, rate-limiting, and
role-grant engine all run regardless — the gates only silence the chat
and proactive-reply paths.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from .config import CFG

log = logging.getLogger(__name__)


# ── Quiet hours ──────────────────────────────────────────────────────

def _parse_hhmm(text: str) -> time | None:
    """Parse 'HH:MM' into a datetime.time. Returns None on bad input."""
    try:
        h, m = text.strip().split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def _get_tz() -> ZoneInfo:
    """Return the configured timezone, or UTC as safe fallback."""
    name = CFG.quiet_hours_timezone or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("Unknown timezone %r in config, falling back to UTC", name)
        return ZoneInfo("UTC")


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    """True if the current local time falls inside the quiet window.

    Handles windows that cross midnight (e.g. 23:00-07:00).
    Returns False if the feature is disabled or the config is malformed.
    """
    if not CFG.quiet_hours_enabled:
        return False

    start = _parse_hhmm(CFG.quiet_hours_start)
    end = _parse_hhmm(CFG.quiet_hours_end)
    if start is None or end is None:
        return False
    if start == end:
        return False  # zero-length window = disabled

    tz = _get_tz()
    current = (now or datetime.now(tz)).astimezone(tz).time()

    if start < end:
        # Normal window: e.g. 13:00–17:00
        return start <= current < end
    else:
        # Wraps midnight: e.g. 23:00–07:00
        return current >= start or current < end


def quiet_status_line() -> str:
    """Human-readable summary for !quiet and presence text."""
    if not CFG.quiet_hours_enabled:
        return "Quiet hours disabled."
    inside = in_quiet_hours()
    tz = _get_tz()
    now = datetime.now(tz).strftime("%H:%M")
    window = f"{CFG.quiet_hours_start}–{CFG.quiet_hours_end} {tz.key}"
    state = "ACTIVE (silent)" if inside else "inactive"
    return f"Quiet hours {state} | window: {window} | now: {now}"


# ── Role allowlist ──────────────────────────────────────────────────

def is_chat_allowed(author: discord.abc.User) -> bool:
    """Check if the author's roles permit Clawy to chat with them.

    Empty allowlist = everyone can chat (the default — matches prior behavior).
    Non-empty allowlist = ONLY members of those named roles get replies.
    Non-Member authors (rare — mostly DMs, which already bail earlier) are
    treated as allowed.
    """
    allowed = set(CFG.chat_allowed_roles)
    if not allowed:
        return True
    if not isinstance(author, discord.Member):
        return True
    return any(r.name in allowed for r in author.roles)


def chat_gate_reason(author: discord.abc.User) -> str | None:
    """If chat is NOT allowed for this author, return a short reason string.
    Returns None if chat IS allowed. For logs / !chatroles status.
    """
    if in_quiet_hours():
        return "quiet hours"
    if not is_chat_allowed(author):
        return "role not in chat allowlist"
    return None
