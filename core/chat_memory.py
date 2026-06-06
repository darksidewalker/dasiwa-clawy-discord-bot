"""
Chat-only memory helpers.

This module deliberately reads and writes only chat_* tables. It prepares a
small, recency-aware memory packet for chat prompts and compresses older turns
into chat_notes so raw history does not grow forever.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Iterable

from .config import CFG
from .expressions import EXPRESSIONS
from .ollama_client import OLLAMA
from .store import STORE

log = logging.getLogger(__name__)

_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_~.-]+):\d+>")
_SHORTCODE_RE = re.compile(r":([A-Za-z0-9_~.-]{2,64}):")
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
)
_VARIATION_SELECTOR = "\ufe0f"
_ZERO_WIDTH_JOINER = "\u200d"


def _is_unicode_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    return any(start <= cp <= end for start, end in _EMOJI_RANGES)


def _emoji_label(token: str) -> str:
    names = []
    for ch in token:
        if ch in {_VARIATION_SELECTOR, _ZERO_WIDTH_JOINER}:
            continue
        try:
            names.append(unicodedata.name(ch).lower().replace(" ", " "))
        except ValueError:
            pass
    return " + ".join(names) or "emoji"


def _describe_named_emoji(name: str) -> str:
    desc = EXPRESSIONS.emoji_description(name)
    if desc:
        return f":{name}: ({desc})"
    return f":{name}:"


def describe_emojis(text: str) -> list[str]:
    """Return compact textual descriptions of emoji present in text."""
    seen: set[str] = set()
    descriptions: list[str] = []

    for match in _CUSTOM_EMOJI_RE.finditer(text):
        desc = _describe_named_emoji(match.group(1))
        if desc not in seen:
            seen.add(desc)
            descriptions.append(desc)

    for match in _SHORTCODE_RE.finditer(text):
        name = match.group(1)
        desc = EXPRESSIONS.emoji_description(name)
        if desc:
            item = f":{name}: ({desc})"
            if item not in seen:
                seen.add(item)
                descriptions.append(item)

    current = ""
    for ch in text:
        if _is_unicode_emoji_char(ch) or ch in {_VARIATION_SELECTOR, _ZERO_WIDTH_JOINER}:
            current += ch
            continue
        if current:
            label = _emoji_label(current)
            if label not in seen:
                seen.add(label)
                descriptions.append(label)
            current = ""
    if current:
        label = _emoji_label(current)
        if label not in seen:
            descriptions.append(label)

    return descriptions[:8]


def normalize_message_text(text: str, *, limit: int = 1000) -> str:
    """Keep original text, plus a short emoji explanation for the LLM."""
    clean = " ".join((text or "").split())
    emojis = describe_emojis(clean)
    if emojis:
        clean = f"{clean} [emoji context: {', '.join(emojis)}]"
    return clean[:limit]


def _format_ts(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "unknown time"


def _age_label(ts: int) -> str:
    delta = max(0, int(time.time()) - int(ts))
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    return f"{delta // 86400} d ago"


def format_turns(turns: Iterable[dict], *, max_chars: int = 260) -> list[str]:
    lines: list[str] = []
    for turn in turns:
        ts = int(turn.get("ts") or 0)
        role = str(turn.get("role") or "?")
        content = str(turn.get("content") or "").replace("\n", " ")
        lines.append(f"[{_age_label(ts)} | {_format_ts(ts)}] {role}: {content[:max_chars]}")
    return lines


async def build_chat_memory_packet(user_id: int, *, recent_limit: int) -> str:
    """Build the memory section handed to the main chat LLM."""
    parts: list[str] = []

    summary = await STORE.get_chat_summary(user_id)
    if summary and summary.get("summary"):
        updated_at = int(summary.get("updated_at") or 0)
        parts.append(
            "Older summarized memory (may be stale; prefer recent turns when they conflict):\n"
            f"[updated {_age_label(updated_at)} | {_format_ts(updated_at)}]\n"
            f"{str(summary['summary'])[:CFG.chat_summary_max_chars]}"
        )

    recent = await STORE.recent_chat_turns(user_id, limit=recent_limit)
    recent_lines = format_turns(recent)
    parts.append(
        "Recent raw turns (highest priority, chronological):\n"
        + ("\n".join(recent_lines) if recent_lines else "(no prior conversation)")
    )

    return "\n\n".join(parts)


async def summarize_old_chat_turns(user_id: int) -> bool:
    """Compress older chat turns into chat_notes and delete only summarized rows."""
    if not CFG.chat_summary_enabled:
        return False

    keep_recent = max(CFG.chat_context_turns, CFG.chat_summary_keep_recent_turns)
    batch_size = max(4, CFG.chat_summary_batch_turns)
    old_turns = await STORE.older_chat_turns(
        user_id,
        keep_recent=keep_recent,
        limit=batch_size,
    )
    if len(old_turns) < min(6, batch_size):
        return False

    existing = await STORE.get_chat_summary(user_id)
    existing_summary = str((existing or {}).get("summary") or "").strip()
    turn_lines = "\n".join(format_turns(old_turns, max_chars=320))

    system = (
        "You compress Discord chat memory for a persona bot. "
        "Use only the supplied chat memory. Do not invent facts. "
        "Return only JSON."
    )
    user = (
        "Update the long-term chat summary for this one user.\n"
        "Rules:\n"
        "- Keep stable preferences, names, ongoing plans, unresolved questions, and tone cues.\n"
        "- Mark old facts as stale when appropriate; do not present them as current.\n"
        "- Drop small talk, repeated greetings, one-off jokes, and resolved details.\n"
        "- Keep it compact and useful for future replies.\n\n"
        f"Existing summary:\n{existing_summary or '(none)'}\n\n"
        f"Older turns to merge:\n{turn_lines}\n\n"
        'Output JSON exactly like: {"summary": "compact memory summary"}'
    )

    try:
        result = await asyncio.wait_for(
            OLLAMA.generate_json(system, user),
            timeout=CFG.ollama_timeout + 2,
        )
    except asyncio.TimeoutError:
        log.warning("chat summarization timed out")
        return False

    if not isinstance(result, dict):
        return False
    summary = str(result.get("summary") or "").strip()
    if not summary:
        return False

    await STORE.upsert_chat_summary(user_id, summary[: CFG.chat_summary_max_chars])
    await STORE.delete_chat_turn_ids([int(t["id"]) for t in old_turns if t.get("id")])
    return True
