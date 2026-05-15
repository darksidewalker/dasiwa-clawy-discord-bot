"""
Deterministic message triggers — post media without invoking the LLM.

When a user's message matches a configured trigger (word/phrase match or regex),
Clawy posts a media item from the media_pool. No LLM call, no roleplay reasoning,
no cost — just a reflex.

Config file: config/triggers.json (hot-reloadable via `!triggers reload` or `!reload`).

Trigger entry shape:
{
  "name":             "f_respect",          // unique-ish label, used for cooldown keying and logs
  "type":             "word" | "regex",     // matching strategy
  "patterns":         ["f", "press f"],     // list — any pattern matching fires the trigger
  "media":            ["f_respect_image"],  // keys from media_pool.json — picked at random if multiple
  "cooldown_seconds": 300,                  // per (trigger, channel), 0 = no cooldown
  "reply_to_user":    true,                 // post as a reply to the user's message (vs plain channel send)
  "case_sensitive":   false,                // regex-only; word triggers are always case-insensitive
  "description":      "F to pay respects"   // optional, shown in !triggers listing
}

Semantics:
- word triggers: case-insensitive, anchored on word boundaries — \\bword\\b.
  So "f" matches "f" or "press F" but NOT "forty", "of", "shaft".
- regex triggers: compiled once at reload time. re.IGNORECASE by default,
  overridable with case_sensitive: true.
- Multiple patterns per trigger: ANY match fires the trigger.
- Multiple triggers can match the same message — only the FIRST in config order
  fires per message (configurable per-message cap via CFG.triggers_max_per_message
  but defaults to 1 to avoid spam).
- Cooldown is keyed on (trigger_name, channel_id) and held in memory — resets
  on bot restart, which is fine for ephemeral reflex semantics.
- Triggers respect chat gating (CFG.chat_enabled, ignored_channels, chat_allowed_roles)
  and pause/sleep state — a paused or sleeping Clawy never fires triggers.

Everything degrades gracefully:
  - malformed trigger entry → logged, skipped, others still load
  - regex that fails to compile → logged, skipped
  - referenced media key missing from media_pool → logged at debug, no post
  - media file/sticker fails to send → logged, no fallback (it's just a reflex)
"""
from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any

import discord

log = logging.getLogger(__name__)

TRIGGERS_PATH = Path(__file__).resolve().parent.parent / "config" / "triggers.json"


class Trigger:
    """A single compiled trigger ready to evaluate."""

    __slots__ = (
        "name", "type", "media", "cooldown_seconds", "reply_to_user",
        "description", "_patterns", "_compiled",
    )

    def __init__(
        self,
        name: str,
        type_: str,
        patterns: list[str],
        media: list[str],
        cooldown_seconds: int,
        reply_to_user: bool,
        case_sensitive: bool,
        description: str,
    ) -> None:
        self.name = name
        self.type = type_
        self.media = media
        self.cooldown_seconds = cooldown_seconds
        self.reply_to_user = reply_to_user
        self.description = description
        self._patterns = patterns

        flags = 0 if case_sensitive else re.IGNORECASE
        compiled: list[re.Pattern[str]] = []
        if type_ == "word":
            # \b is Unicode-aware in Python 3 by default; we explicitly escape
            # the pattern body so users can type "press f" without worrying
            # about regex metachars in their plain word phrases.
            for p in patterns:
                escaped = re.escape(p)
                # Word boundaries only apply meaningfully to alphanumeric
                # endpoints. For phrases like "press f" the boundary on the
                # ASCII "f" still works. For pattern bodies starting/ending
                # with non-word chars, \b is a no-op — acceptable.
                compiled.append(re.compile(rf"\b{escaped}\b", flags=flags | re.IGNORECASE))
        elif type_ == "regex":
            for p in patterns:
                compiled.append(re.compile(p, flags=flags))
        else:
            raise ValueError(f"unknown trigger type {type_!r}")
        self._compiled = compiled

    def matches(self, text: str) -> bool:
        return any(rgx.search(text) for rgx in self._compiled)

    def pattern_summary(self) -> str:
        """Short human-readable summary for !triggers listing."""
        items = self._patterns[:3]
        suffix = f" (+{len(self._patterns) - 3} more)" if len(self._patterns) > 3 else ""
        return ", ".join(repr(p) for p in items) + suffix


class TriggersManager:
    """Loads triggers, evaluates matches, tracks per-channel cooldowns."""

    def __init__(self, path: Path = TRIGGERS_PATH) -> None:
        self.path = path
        self._lock = Lock()
        self._triggers: list[Trigger] = []
        # cooldown key: (trigger_name, channel_id) → epoch seconds of last fire
        self._last_fired: dict[tuple[str, int], float] = {}
        self.reload()

    # ---------- io ----------
    def reload(self) -> int:
        """Reload triggers.json. Returns count of successfully loaded triggers.

        Cooldown state is preserved across reloads — a trigger that was on
        cooldown stays on cooldown until its window expires, even if its
        config was edited. This is intentional: you don't get to skip a
        cooldown by tweaking the file.
        """
        import json
        with self._lock:
            if not self.path.exists():
                self._triggers = []
                return 0
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = (
                data.get("triggers")
                if isinstance(data, dict) and "triggers" in data
                else (data if isinstance(data, list) else [])
            )
            triggers: list[Trigger] = []
            for i, entry in enumerate(raw):
                if not isinstance(entry, dict):
                    log.warning("triggers[%d] is not an object — skipped", i)
                    continue
                name = str(entry.get("name", f"trigger_{i}")).strip()
                t = entry.get("type", "word")
                if t not in {"word", "regex"}:
                    log.warning("triggers[%s] has invalid type %r — skipped", name, t)
                    continue
                patterns = entry.get("patterns") or []
                if isinstance(patterns, str):
                    patterns = [patterns]
                patterns = [str(p) for p in patterns if isinstance(p, str) and p.strip()]
                if not patterns:
                    log.warning("triggers[%s] has no patterns — skipped", name)
                    continue
                media = entry.get("media") or []
                if isinstance(media, str):
                    media = [media]
                media = [str(m) for m in media if isinstance(m, str) and m.strip()]
                if not media:
                    log.warning("triggers[%s] has no media keys — skipped", name)
                    continue
                try:
                    triggers.append(Trigger(
                        name=name,
                        type_=t,
                        patterns=patterns,
                        media=media,
                        cooldown_seconds=int(entry.get("cooldown_seconds", 300)),
                        reply_to_user=bool(entry.get("reply_to_user", True)),
                        case_sensitive=bool(entry.get("case_sensitive", False)),
                        description=str(entry.get("description", "")),
                    ))
                except (re.error, ValueError) as e:
                    log.warning("triggers[%s] failed to compile: %s", name, e)
                    continue
            self._triggers = triggers
            return len(triggers)

    # ---------- introspection ----------
    def list_triggers(self) -> list[Trigger]:
        with self._lock:
            return list(self._triggers)

    def count(self) -> int:
        with self._lock:
            return len(self._triggers)

    # ---------- matching + cooldown ----------
    def find_match(
        self,
        text: str,
        channel_id: int,
        *,
        skip: set[str] | None = None,
    ) -> Trigger | None:
        """Return the FIRST trigger matching `text` whose cooldown has expired.

        Triggers still on cooldown are skipped silently — a later trigger may
        match instead. This means a single message can still produce a reflex
        even if its closest match is on cooldown.

        `skip`: optional set of trigger names to exclude (used to find a
        second match on the same message when max_per_message > 1).
        """
        if not text:
            return None
        now = time.time()
        with self._lock:
            for trig in self._triggers:
                if skip is not None and trig.name in skip:
                    continue
                if not trig.matches(text):
                    continue
                last = self._last_fired.get((trig.name, channel_id), 0.0)
                if trig.cooldown_seconds > 0 and (now - last) < trig.cooldown_seconds:
                    continue
                return trig
        return None

    def mark_fired(self, trigger_name: str, channel_id: int) -> None:
        """Record that a trigger fired in a channel. Call after successful post."""
        with self._lock:
            self._last_fired[(trigger_name, channel_id)] = time.time()

    def cooldown_remaining(self, trigger_name: str, channel_id: int) -> int:
        """Seconds remaining on cooldown for a (trigger, channel). 0 if ready."""
        with self._lock:
            for trig in self._triggers:
                if trig.name != trigger_name:
                    continue
                last = self._last_fired.get((trigger_name, channel_id), 0.0)
                remaining = trig.cooldown_seconds - int(time.time() - last)
                return max(0, remaining)
        return 0


TRIGGERS = TriggersManager()


# ============================================================
# Firing — the single place where a matched trigger posts media
# ============================================================

async def fire_trigger(
    trigger: Trigger,
    message: discord.Message,
) -> bool:
    """Post a randomly-chosen media item for a matched trigger.

    Returns True if anything was posted (and cooldown should advance),
    False on any failure — caller logs and does not advance cooldown.
    """
    # Lazy import to avoid the expressions module pulling discord at
    # core-package import time when discord isn't yet ready in tests.
    from .expressions import EXPRESSIONS, _build_attach_file

    if not trigger.media:
        return False

    # Random pick — over many fires this surfaces all media items.
    media_key = random.choice(trigger.media)
    entry = EXPRESSIONS.media_entry(media_key)
    if entry is None:
        log.info(
            "trigger %r matched but references missing media key %r — "
            "check that the key exists in media_pool.json",
            trigger.name, media_key,
        )
        return False

    channel = message.channel
    guild = message.guild
    reference = message if trigger.reply_to_user else None

    send_kwargs: dict[str, Any] = {}
    if reference is not None:
        send_kwargs["reference"] = reference
        send_kwargs["mention_author"] = False

    media_type = entry.get("type")
    if media_type == "sticker":
        sid = entry.get("sticker_id")
        if guild is None or not sid:
            log.info(
                "trigger %r matched but sticker entry %r has no sticker_id "
                "(or no guild context)",
                trigger.name, media_key,
            )
            return False
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            log.warning("trigger %r: invalid sticker_id %r", trigger.name, sid)
            return False
        sticker = discord.utils.get(guild.stickers, id=sid_int)
        if sticker is None:
            log.info(
                "trigger %r matched but sticker id %s not found in guild — "
                "is the sticker still on the server?",
                trigger.name, sid_int,
            )
            return False
        send_kwargs["stickers"] = [sticker]

    elif media_type in {"file", "url"}:
        file_obj = await _build_attach_file(entry)
        if file_obj is None:
            log.info(
                "trigger %r matched but media %r failed to load — "
                "check file path or URL in media_pool.json",
                trigger.name, media_key,
            )
            return False
        send_kwargs["file"] = file_obj

    else:
        log.warning(
            "trigger %r: media %r has unsupported type %r",
            trigger.name, media_key, media_type,
        )
        return False

    try:
        await channel.send(**send_kwargs)
        return True
    except discord.DiscordException as e:
        log.warning("trigger %r send failed: %s", trigger.name, e)
        return False
