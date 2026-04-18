"""
Pre-filter messages BEFORE hitting the LLM.

Returns one of:
  - ("skip",    reason)   -> don't involve the LLM at all (e.g. protected user, ignored channel)
  - ("action",  {...})    -> take this action directly without asking the LLM (e.g. blocklist hit)
  - ("llm",     None)     -> pass to the LLM for a decision

The hard blocklist is OPT-IN: disabled by default. Enable by setting
`moderation.blocklist_enabled: true` in config.yaml AND providing a
`config/blocklist.json` file with words/phrases.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from threading import Lock
from typing import Any

import discord

from .config import CFG
from .store import STORE
from .tracking import SPAM

log = logging.getLogger(__name__)


# ── Blocklist loader ─────────────────────────────────────────────────

class BlocklistLoader:
    """Lazy-loads and caches the blocklist file contents. Thread-safe."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._words: list[str] = []
        self._phrases: list[str] = []
        self._timeout_seconds: int = 600
        self._notify_user: bool = True
        self._notify_message: str = (
            "That kind of language is not tolerated here. You have been silenced."
        )

    def reload(self, path_str: str) -> int:
        """Load or reload the blocklist file. Returns total entry count."""
        with self._lock:
            path = Path(path_str)
            if not path.exists():
                self._words = []
                self._phrases = []
                log.info("Blocklist file not found at %s — blocklist is empty", path_str)
                return 0
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._words = [w.lower() for w in data.get("words", []) if w]
                self._phrases = [p.lower() for p in data.get("phrases", []) if p]
                self._timeout_seconds = int(data.get("timeout_seconds", 600))
                self._notify_user = bool(data.get("notify_user", True))
                self._notify_message = str(
                    data.get("notify_message", self._notify_message)
                )
                total = len(self._words) + len(self._phrases)
                log.info(
                    "Blocklist loaded: %d words, %d phrases",
                    len(self._words), len(self._phrases),
                )
                return total
            except Exception as e:
                log.error("Failed to load blocklist %s: %s", path_str, e)
                self._words = []
                self._phrases = []
                return 0

    def check(self, content: str) -> str | None:
        """Return the first matching word/phrase, or None."""
        with self._lock:
            if not self._words and not self._phrases:
                return None
            lowered = content.lower()
            for w in self._words:
                if re.search(rf"\b{re.escape(w)}\b", lowered):
                    return w
            for p in self._phrases:
                if p in lowered:
                    return p
        return None

    @property
    def timeout_seconds(self) -> int:
        return self._timeout_seconds

    @property
    def notify_user(self) -> bool:
        return self._notify_user

    @property
    def notify_message(self) -> str:
        return self._notify_message


BLOCKLIST = BlocklistLoader()


def _blocklist_enabled() -> bool:
    return bool(CFG.mod.get("blocklist_enabled", False))


def _blocklist_path() -> str:
    return str(CFG.mod.get("blocklist_file", "config/blocklist.json"))


# Load once at import time if enabled
if _blocklist_enabled():
    BLOCKLIST.reload(_blocklist_path())


# ── Main prefilter ───────────────────────────────────────────────────

async def prefilter(message: discord.Message, bot_user_id: int) -> tuple[str, Any]:
    # 0. Ignore DMs and bots (including self)
    if message.author.bot:
        return ("skip", "author is a bot")
    if message.guild is None:
        return ("skip", "DM")

    # 1. Ignored channels
    if message.channel.name in CFG.ignored_channels:
        return ("skip", "ignored channel")

    # 2. Owner is untouchable
    if message.author.id == CFG.owner_id:
        if bot_user_id in [u.id for u in message.mentions]:
            return ("llm", None)
        return ("skip", "owner message, no mention")

    # 3. Protected-role users: never punish, but allow replies when mentioned
    author_roles = {r.name for r in getattr(message.author, "roles", [])}
    is_protected = bool(author_roles & set(CFG.protected_roles))
    if is_protected and bot_user_id not in [u.id for u in message.mentions]:
        return ("skip", "protected user, not mentioned")

    # 4. Hard blocklist (opt-in via moderation.blocklist_enabled)
    if _blocklist_enabled() and not is_protected:
        hit = BLOCKLIST.check(message.content)
        if hit:
            return (
                "action",
                {
                    "action": "timeout",
                    "reason": f"blocklist term: {hit}",
                    "duration_seconds": BLOCKLIST.timeout_seconds,
                    "also_delete": True,
                    "source": "prefilter:blocklist",
                    "notify_user": BLOCKLIST.notify_user,
                    "notify_message": BLOCKLIST.notify_message,
                },
            )

    # 5. Spam detection: record this message, then check the rate
    SPAM.record(message.author.id)
    if SPAM.is_spamming(message.author.id) and not is_protected:
        # Escalate to mute + delete once the user has accumulated enough
        # strikes within the rolling window. Below threshold → warn (which
        # itself adds a strike, so repeat offenders converge on the mute).
        strike_threshold = int(CFG.mod.get("spam_strike_threshold", 3))
        window_hours = int(CFG.mod.get("strike_window_hours", 24))
        strikes = await STORE.count_strikes(message.author.id, window_hours)
        if strikes >= strike_threshold:
            return (
                "action",
                {
                    "action": "timeout",
                    "reason": f"spam — escalated after {strikes} strikes",
                    "duration_seconds": int(
                        CFG.mod.get("spam_timeout_seconds", 600)
                    ),
                    "also_delete": True,
                    "source": "prefilter:spam",
                },
            )
        return (
            "action",
            {
                "action": "warn",
                "reason": "message rate exceeded",
                "message": "Please slow down — you're sending messages too fast.",
                "source": "prefilter:spam",
            },
        )

    # 6. Otherwise, send to LLM
    return ("llm", None)
