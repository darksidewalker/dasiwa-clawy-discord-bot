"""
Expressive output: emoji reactions, stickers, and media attachments.

Two JSON configs (hot-reloadable like personas.json):

  config/emoji_mapping.json — descriptions of custom guild emoji and any
    standard Unicode emoji Clawy should know about. The LLM uses the
    *description* to decide when to use a given emoji.

  config/media_pool.json — named pool of stickers and media files Clawy
    can attach to a message. Each entry has a `type` of "sticker", "file",
    or "url", plus a description.

The LLM optionally returns these fields in its JSON response:

  "react":    list of emoji names or Unicode characters
  "sticker":  key from media_pool (type=sticker only)
  "attach":   key from media_pool (type=file or type=url)

The send_with_extras() helper handles message + reaction + sticker +
attachment in one place, so all four call sites (executor reply/warn,
moderation chat, admin jumpin) stay consistent.

Everything degrades gracefully:
  - missing emoji → skipped, logged at debug
  - missing media key → skipped, logged at debug
  - file path missing on disk → skipped, logged at warning
  - master switch (CFG.expressions_enabled) off → nothing is sent

All behavior is best-effort: a failed reaction never breaks the main reply.
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from threading import Lock
from typing import Any

import discord

log = logging.getLogger(__name__)

EMOJI_PATH = Path(__file__).resolve().parent.parent / "config" / "emoji_mapping.json"
MEDIA_PATH = Path(__file__).resolve().parent.parent / "config" / "media_pool.json"

# Match :emoji_name: shortcodes the LLM might emit inside a message.
# We strip these from the visible text and convert them into reactions instead,
# since Discord won't render :name: text as the actual emoji from the API.
_SHORTCODE_RE = re.compile(r":([a-zA-Z0-9_]+):")

# Discord caps at 20 reactions per message, but more than ~3 looks spammy.
_HARD_REACTION_CAP = 5

# Cap how many media items to fetch from a URL — 8 MB matches Discord's
# free-tier upload limit. We never attach anything bigger.
_MAX_REMOTE_BYTES = 8 * 1024 * 1024


class ExpressionsManager:
    """Holds the emoji mapping and media pool, both hot-reloadable."""

    def __init__(self, emoji_path: Path = EMOJI_PATH, media_path: Path = MEDIA_PATH) -> None:
        self.emoji_path = emoji_path
        self.media_path = media_path
        self._lock = Lock()
        self._emoji: dict[str, str] = {}    # name -> description
        self._media: dict[str, dict] = {}    # key -> {type, ..., description}
        self.reload()

    # ---------- io ----------
    def reload(self) -> tuple[int, int]:
        """Reload both JSON files. Returns (emoji_count, media_count).

        Missing files are treated as empty (not an error). Malformed files
        raise, since silent corruption would be worse than a startup failure.
        """
        with self._lock:
            self._emoji = self._load_emoji(self.emoji_path)
            self._media = self._load_media(self.media_path)
            return len(self._emoji), len(self._media)

    @staticmethod
    def _load_emoji(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Expected shape: { "emoji": { "name": "description", ... } }
        # Fallback: top-level dict is the map.
        if isinstance(data, dict) and "emoji" in data and isinstance(data["emoji"], dict):
            raw = data["emoji"]
        else:
            raw = data if isinstance(data, dict) else {}
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}

    @staticmethod
    def _load_media(path: Path) -> dict[str, dict]:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Expected shape: { "media": { "key": {type, ...}, ... } }
        # Fallback: top-level dict is the map.
        if isinstance(data, dict) and "media" in data and isinstance(data["media"], dict):
            raw = data["media"]
        else:
            raw = data if isinstance(data, dict) else {}
        out: dict[str, dict] = {}
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            t = v.get("type")
            if t not in {"sticker", "file", "url"}:
                log.warning("media_pool entry %r has invalid type %r — skipped", k, t)
                continue
            out[str(k)] = v
        return out

    # ---------- readers ----------
    def emoji_names(self) -> list[str]:
        with self._lock:
            return list(self._emoji.keys())

    def media_keys(self) -> list[str]:
        with self._lock:
            return list(self._media.keys())

    def emoji_description(self, name: str) -> str | None:
        with self._lock:
            return self._emoji.get(name)

    def media_entry(self, key: str) -> dict | None:
        with self._lock:
            entry = self._media.get(key)
            return dict(entry) if entry else None

    def sample_for_prompt(self, limit: int) -> tuple[dict[str, str], dict[str, dict]]:
        """Random subset of emoji + media for the system prompt.

        Returns (sampled_emoji, sampled_media). When a pool is smaller than
        the limit, the full pool is returned. Sampling is random per call
        so Clawy doesn't fixate on the first N items.
        """
        with self._lock:
            emoji_items = list(self._emoji.items())
            media_items = list(self._media.items())
        if len(emoji_items) > limit:
            emoji_items = random.sample(emoji_items, limit)
        if len(media_items) > limit:
            media_items = random.sample(media_items, limit)
        return dict(emoji_items), dict(media_items)


EXPRESSIONS = ExpressionsManager()


# ============================================================
# Prompt-building helpers
# ============================================================

def build_expressions_prompt_block(
    *,
    allow_reactions: bool,
    allow_stickers: bool,
    allow_attachments: bool,
    prompt_limit: int,
    max_reactions_per_message: int,
) -> str:
    """Build the prompt section that tells the LLM what's available.

    Empty string if nothing is allowed or pools are empty — the field is
    simply not advertised, and the LLM won't know to use it.
    """
    if not (allow_reactions or allow_stickers or allow_attachments):
        return ""

    sampled_emoji, sampled_media = EXPRESSIONS.sample_for_prompt(prompt_limit)

    sections: list[str] = []

    if allow_reactions and sampled_emoji:
        lines = [f"  :{name}: — {desc}" for name, desc in sampled_emoji.items()]
        sections.append(
            "REACTIONS — you may include a \"react\" field (list of strings, "
            f"max {max_reactions_per_message}) to react to the user's message with "
            "emoji. Use sparingly and only when it fits the moment. You can mix "
            "Unicode emoji (e.g. \"💀\", \"🔥\") with named custom emoji from this list:\n"
            + "\n".join(lines)
        )

    stickers = {k: v for k, v in sampled_media.items() if v.get("type") == "sticker"}
    if allow_stickers and stickers:
        lines = [f"  {k} — {v.get('description', '(no description)')}" for k, v in stickers.items()]
        sections.append(
            "STICKERS — you may include a \"sticker\" field (single key from the list "
            "below) to attach a Discord sticker. Use rarely, for emphasis or punchlines:\n"
            + "\n".join(lines)
        )

    media = {k: v for k, v in sampled_media.items() if v.get("type") in {"file", "url"}}
    if allow_attachments and media:
        lines = [f"  {k} — {v.get('description', '(no description)')}" for k, v in media.items()]
        sections.append(
            "MEDIA ATTACHMENTS — you may include an \"attach\" field (single key from "
            "the list below) to post an image, video, or GIF along with your message:\n"
            + "\n".join(lines)
        )

    if not sections:
        return ""

    return (
        "\n\nEXPRESSIVE OUTPUT (all optional)\n"
        "These fields are NEVER required. Only use them when they genuinely "
        "add to the moment. Most messages should have none. The \"message\" "
        "field remains the primary output.\n\n"
        + "\n\n".join(sections)
    )


def schema_extension_doc() -> str:
    """Short schema doc snippet appended to ACTION_SCHEMA_DOC and chat schema."""
    return (
        '  "react":   array of strings,  // optional — emoji to react with\n'
        '  "sticker": string,            // optional — key from sticker list\n'
        '  "attach":  string,            // optional — key from media list\n'
    )


# ============================================================
# Send helper — the single place where reactions / stickers / media post
# ============================================================

def _resolve_emoji(
    guild: discord.Guild | None,
    name_or_char: str,
) -> str | discord.Emoji | None:
    """Resolve a token from the LLM's react list to something Discord accepts.

    - Unicode character → returned as-is (Discord's add_reaction accepts it).
    - Bare name like "catjam" → looked up in the guild's custom emoji.
    - Discord shortcode like ":catjam:" → stripped and looked up.
    - Animated/static custom emoji string "<a:name:id>" or "<:name:id>" → parsed
      and returned via discord.PartialEmoji.from_str if the guild has it.

    Returns None if nothing matches; caller logs and skips.
    """
    if not name_or_char:
        return None
    s = name_or_char.strip()
    if not s:
        return None

    # Already a full emoji tag — let discord.py parse it.
    if s.startswith("<") and s.endswith(">"):
        try:
            return discord.PartialEmoji.from_str(s)
        except Exception:
            return None

    # Strip surrounding colons if present.
    if s.startswith(":") and s.endswith(":") and len(s) > 2:
        s = s[1:-1]

    # If it has no ASCII letters/digits/underscores, treat it as raw Unicode.
    if not re.fullmatch(r"[A-Za-z0-9_]+", s):
        return s  # Unicode emoji — pass through

    # Named custom emoji — needs guild lookup.
    if guild is None:
        return None
    for e in guild.emojis:
        if e.name == s:
            return e
    return None


async def _build_attach_file(entry: dict) -> discord.File | None:
    """Turn a media-pool entry (type=file or type=url) into a discord.File.

    file: read the local path. We do NOT path-traverse-protect here — the
    media_pool.json is owner-controlled config, not user input.

    url: fetch via aiohttp, capped at _MAX_REMOTE_BYTES. Returns None on
    any failure (network error, oversized, missing file).
    """
    t = entry.get("type")
    if t == "file":
        p = entry.get("path")
        if not p:
            return None
        path = Path(p)
        if not path.is_absolute():
            # Resolve relative to project root (parent of /core/).
            path = Path(__file__).resolve().parent.parent / p
        if not path.exists() or not path.is_file():
            log.warning("media pool: file not found at %s", path)
            return None
        try:
            return discord.File(str(path), filename=path.name)
        except Exception as e:
            log.warning("media pool: discord.File failed for %s: %s", path, e)
            return None

    if t == "url":
        url = entry.get("url")
        if not url:
            return None
        try:
            import aiohttp  # lazy: only needed for url-type media
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        log.warning("media pool: url %s returned %s", url, resp.status)
                        return None
                    data = await resp.content.read(_MAX_REMOTE_BYTES + 1)
                    if len(data) > _MAX_REMOTE_BYTES:
                        log.warning("media pool: url %s exceeds size cap", url)
                        return None
            # Best-effort filename from URL tail.
            name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "attachment"
            import io
            return discord.File(io.BytesIO(data), filename=name)
        except Exception as e:
            log.warning("media pool: url fetch failed for %s: %s", url, e)
            return None

    return None


def _coerce_react_list(raw: Any, cap: int) -> list[str]:
    """Normalize whatever the LLM returned in 'react' into a clean list.

    Accepts: list[str], single str, None. Strips empties, dedupes, caps length.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = [str(x) for x in raw if isinstance(x, (str, int))]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        item = item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= cap:
            break
    return out


def _strip_shortcodes_to_reactions(text: str) -> tuple[str, list[str]]:
    """Pull :emoji_name: shortcodes out of the visible text.

    Discord doesn't render :name: as the actual emoji via API send. Rather
    than leaving them as ugly literal text, we strip them and queue them as
    reactions instead. Returns (cleaned_text, extracted_names).
    """
    extracted: list[str] = []

    def _grab(m: re.Match[str]) -> str:
        extracted.append(m.group(1))
        return ""

    cleaned = _SHORTCODE_RE.sub(_grab, text)
    # Collapse any double-spaces introduced by stripping.
    cleaned = re.sub(r"  +", " ", cleaned).strip()
    return cleaned, extracted


async def send_with_extras(
    channel: discord.abc.Messageable,
    text: str,
    result: dict[str, Any],
    *,
    cfg,
    reference: discord.Message | None = None,
    mention_author: bool = False,
) -> discord.Message | None:
    """Send a message and optionally attach reactions, a sticker, and media.

    `result` is the raw LLM dict — we look up react/sticker/attach in it.
    `cfg` is the CFG singleton — we honor its expressions flags.

    Returns the sent message, or None if the primary send failed. Reaction
    and post-send extras failures are silent (logged at debug/warning).
    """
    guild = getattr(channel, "guild", None)

    # ---- Reactions ----
    react_tokens: list[str] = []
    if cfg.expressions_enabled and cfg.expressions_allow_reactions:
        cap = cfg.expressions_max_reactions
        react_tokens = _coerce_react_list(result.get("react"), cap)

    # Also pull any :shortcode: emoji from the message body and convert
    # them into reactions — Discord won't render them inline via API send.
    text_clean = text or ""
    if cfg.expressions_enabled and cfg.expressions_allow_reactions and text_clean:
        text_clean, inline_names = _strip_shortcodes_to_reactions(text_clean)
        # Merge with explicit react tokens; respect cap.
        remaining = cfg.expressions_max_reactions - len(react_tokens)
        if remaining > 0:
            for n in inline_names:
                if n in react_tokens:
                    continue
                react_tokens.append(n)
                remaining -= 1
                if remaining <= 0:
                    break

    # ---- Sticker ----
    sticker_obj: discord.GuildSticker | discord.StickerItem | None = None
    if cfg.expressions_enabled and cfg.expressions_allow_stickers:
        sticker_key = result.get("sticker")
        if isinstance(sticker_key, str) and sticker_key.strip():
            entry = EXPRESSIONS.media_entry(sticker_key.strip())
            if entry and entry.get("type") == "sticker":
                sid = entry.get("sticker_id")
                if guild is not None and sid:
                    try:
                        sid_int = int(sid)
                        sticker_obj = discord.utils.get(guild.stickers, id=sid_int)
                        if sticker_obj is None:
                            # Try the bot's available stickers (cross-guild stickers
                            # the bot has access to via Use External Stickers).
                            log.debug("sticker %s (id %s) not in guild stickers", sticker_key, sid_int)
                    except (TypeError, ValueError):
                        log.warning("media pool: invalid sticker_id for %s", sticker_key)

    # ---- File attachment ----
    file_obj: discord.File | None = None
    if cfg.expressions_enabled and cfg.expressions_allow_attachments:
        attach_key = result.get("attach")
        if isinstance(attach_key, str) and attach_key.strip():
            entry = EXPRESSIONS.media_entry(attach_key.strip())
            if entry and entry.get("type") in {"file", "url"}:
                file_obj = await _build_attach_file(entry)

    # ---- Send the message ----
    # Discord requires that send() get either content OR sticker OR file —
    # an empty content is OK as long as at least one of the three is present.
    if not text_clean and sticker_obj is None and file_obj is None:
        # Nothing to actually post (e.g. text was nothing but shortcodes that
        # all converted to reactions). Skip the send entirely — but still
        # apply reactions to the original message if there's a reference.
        if react_tokens and reference is not None:
            await _apply_reactions(reference, react_tokens)
        return None

    send_kwargs: dict[str, Any] = {}
    if text_clean:
        send_kwargs["content"] = text_clean[:1800]
    if reference is not None:
        send_kwargs["reference"] = reference
        send_kwargs["mention_author"] = mention_author
    if sticker_obj is not None:
        send_kwargs["stickers"] = [sticker_obj]
    if file_obj is not None:
        send_kwargs["file"] = file_obj

    try:
        sent = await channel.send(**send_kwargs)
    except discord.DiscordException as e:
        log.warning("send_with_extras: primary send failed: %s", e)
        # Best-effort fallback: try again without extras (sticker/file
        # may have caused permission errors), so the user at least gets text.
        if (sticker_obj is not None or file_obj is not None) and text_clean:
            try:
                sent = await channel.send(
                    content=text_clean[:1800],
                    reference=reference,
                    mention_author=mention_author,
                )
            except discord.DiscordException as e2:
                log.warning("send_with_extras: text-only fallback also failed: %s", e2)
                return None
        else:
            return None

    # ---- Apply reactions to the new message (NOT the reference) ----
    # Reactions go on what Clawy just said, expressing her own punctuation.
    if react_tokens:
        await _apply_reactions(sent, react_tokens)

    return sent


async def _apply_reactions(message: discord.Message, tokens: list[str]) -> None:
    """Best-effort: add each reaction. Per-token failures are silent."""
    guild = message.guild
    for tok in tokens:
        emoji = _resolve_emoji(guild, tok)
        if emoji is None:
            log.debug("could not resolve emoji %r", tok)
            continue
        try:
            await message.add_reaction(emoji)
        except discord.Forbidden:
            log.debug("missing Add Reactions permission")
            return  # No point trying further tokens.
        except discord.HTTPException as e:
            log.debug("add_reaction failed for %r: %s", tok, e)


# ============================================================
# Standalone reactor — for the moderation pipeline to react WITHOUT replying
# ============================================================

async def react_to(
    message: discord.Message,
    tokens: list[str] | str | None,
    *,
    cap: int,
) -> int:
    """React to an existing message with a list of emoji tokens.

    Used by the moderation pipeline when the LLM picks `ignore` but still
    returns a `react` field — Clawy can "acknowledge" a message non-verbally.
    Returns the number of reactions successfully applied.
    """
    react_tokens = _coerce_react_list(tokens, cap)
    if not react_tokens:
        return 0
    applied = 0
    guild = message.guild
    for tok in react_tokens:
        emoji = _resolve_emoji(guild, tok)
        if emoji is None:
            continue
        try:
            await message.add_reaction(emoji)
            applied += 1
        except discord.Forbidden:
            return applied
        except discord.HTTPException:
            continue
    return applied
