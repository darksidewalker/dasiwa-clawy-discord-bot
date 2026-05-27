"""
Main event listener.

Depending on CFG.mode, each message goes through one or both of:
  - MODERATION pipeline: prefilter -> LLM action -> executor
  - CHAT pipeline:       persona reply using chat_turns memory from DB

Moderation memory (strikes, events) and chat memory (turns) are stored in
separate SQLite tables and never cross.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from datetime import timedelta, datetime, timezone

import discord
from discord.ext import commands

from core.config import CFG
from core.executor import execute
from core.expressions import send_with_extras
from core.gating import in_quiet_hours, is_chat_allowed
from core.triggers import TRIGGERS, fire_trigger
from core.ollama_client import OLLAMA
from core.persona import PERSONAS
from core.prefilter import prefilter
from core.prompts import build_chat_system_prompt, build_system_prompt
from core.store import STORE
from core.tracking import MENTION_RL, SPAM

log = logging.getLogger(__name__)


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # per-channel rolling "what was said recently" — fed to the moderation LLM
        self._channel_ctx: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=10))
        # per-channel last proactive reply timestamp (for cooldown)
        self._last_proactive: dict[int, float] = defaultdict(float)
        # users whose Discord profile has already been mined this session
        # (in-memory only — resets on bot restart, which is fine)
        self._profile_mined: set[int] = set()

    # ================================================================
    # MAIN EVENT
    # ================================================================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Don't intercept command messages
        if message.content.startswith(CFG.command_prefix):
            return
        if message.guild is None or message.author.bot:
            return
        if CFG.guild_id and message.guild.id != CFG.guild_id:
            return
        if message.channel.name in CFG.ignored_channels:
            return

        # Track "seen users" in DB — also mine Discord profile once per session
        try:
            await self._touch_and_mine(message)
        except Exception as e:
            log.warning("touch_user failed: %s", e)

        # Rolling channel context (short-term, in-memory)
        self._channel_ctx[message.channel.id].append(
            f"{message.author.display_name}: {message.content[:120]}"
        )

        if CFG.state.paused:
            return

        # Sleep mode — ignore everything except admin commands (handled elsewhere)
        if CFG.state.sleeping:
            return

        bot_user_id = self.bot.user.id if self.bot.user else 0
        was_mentioned = bot_user_id in [u.id for u in message.mentions]

        # ========== OWNER SHORTCUT ==========
        # Owner always goes straight to chat with full submission dynamic.
        # Never run through moderation LLM — they are untouchable and above judgment.
        # This check is placed early to exempt the owner from rate limits and moderation.
        if message.author.id == CFG.owner_id:
            addressed = was_mentioned or self._addresses_bot(message)
            log.info("owner detected: %s | mentioned=%s | addresses_bot=%s | addressed=%s | chat_enabled=%s",
                     message.author.display_name, was_mentioned, self._addresses_bot(message),
                     addressed, CFG.chat_enabled)
            if addressed:
                if not in_quiet_hours() and CFG.chat_enabled:
                    await self._chat(message)
                else:
                    log.info("owner shortcut blocked: quiet=%s chat_enabled=%s",
                             in_quiet_hours(), CFG.chat_enabled)
            return

        # ========== MENTION RATE LIMIT ==========
        # Checked before anything else — applies regardless of mode.
        if was_mentioned and not CFG.state.paused:
            MENTION_RL.record(message.author.id)
            verdict = MENTION_RL.check(message.author.id)
            if verdict == "warn":
                try:
                    warn_msg = await message.channel.send(
                        f"{message.author.mention} Careful — you are pinging me rather often. "
                        f"Keep it up and I will have to silence you for a while.",
                        reference=message,
                        mention_author=False,
                    )
                    # Auto-delete the warning after 12 seconds
                    await asyncio.sleep(12)
                    await warn_msg.delete()
                except discord.DiscordException:
                    pass
                await STORE.log_mod_event(
                    user_id=message.author.id, kind="warn",
                    reason="mention spam — rate limit warning",
                    source="mention_rl",
                    channel_id=message.channel.id, message_id=message.id,
                )
                return  # Don't process this message further
            elif verdict == "timeout":
                import datetime as _dt
                dur = MENTION_RL.timeout_duration()
                until = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=dur)
                if isinstance(message.author, discord.Member):
                    try:
                        await message.author.timeout(
                            until, reason="mention spam — rate limit escalation"
                        )
                    except discord.Forbidden:
                        pass
                try:
                    mute_msg = await message.channel.send(
                        f"{message.author.mention} You have been silenced for "
                        f"{dur // 60} minute(s). Consider this a lesson in patience.",
                        mention_author=False,
                    )
                    await asyncio.sleep(12)
                    await mute_msg.delete()
                except discord.DiscordException:
                    pass
                await STORE.log_mod_event(
                    user_id=message.author.id, kind="timeout",
                    reason="mention spam — rate limit escalation",
                    source="mention_rl",
                    channel_id=message.channel.id, message_id=message.id,
                    extra=f'{{"duration_seconds": {dur}}}',
                )
                return

        # ========== TRIGGERS (deterministic, no LLM) ==========
        # Keyword/regex triggers from config/triggers.json. They fire as a
        # parallel reflex: matched media posts immediately, and the message
        # also continues through the normal chat/mod paths below. Triggers
        # respect chat gating (allowlist, ignored channels) since they
        # share the same "is Clawy allowed to speak here?" semantics.
        if CFG.triggers_enabled and CFG.chat_enabled:
            if is_chat_allowed(message.author):
                log.info(
                    "trigger eval: text=%r channel=%s author=%s loaded=%d",
                    message.content[:80],
                    getattr(message.channel, "name", "?"),
                    message.author.display_name,
                    TRIGGERS.count(),
                )
                fired = 0
                # Cap: usually 1, but configurable in case you want multiple.
                # We loop with a temporary skip-set so we don't fire the same
                # trigger twice in one message, and so we walk past cooldown
                # collisions to find another match.
                fired_names: set[str] = set()
                remaining = CFG.triggers_max_per_message
                while remaining > 0:
                    text = message.content
                    trig = TRIGGERS.find_match(
                        text, message.channel.id, skip=fired_names,
                    )
                    if trig is None:
                        log.info("trigger eval: no match for text=%r", text[:80])
                        break
                    log.info("trigger eval: matched %r — firing", trig.name)
                    try:
                        if await fire_trigger(trig, message):
                            TRIGGERS.mark_fired(trig.name, message.channel.id)
                            fired += 1
                            fired_names.add(trig.name)
                    except Exception as e:
                        log.warning("trigger %r raised: %s", trig.name, e)
                        fired_names.add(trig.name)  # don't retry the same one
                    remaining -= 1
                if fired:
                    log.info(
                        "fired %d trigger(s) on message %s in #%s by %s",
                        fired, message.id,
                        getattr(message.channel, "name", "?"),
                        message.author.display_name,
                    )
            else:
                log.info(
                    "trigger eval skipped: chat not allowed for %s",
                    message.author.display_name,
                )
        else:
            log.info(
                "trigger eval skipped: triggers_enabled=%s chat_enabled=%s",
                CFG.triggers_enabled, CFG.chat_enabled,
            )

        # ========== OWNER SHORTCUT ==========
        # Owner always goes straight to chat with full submission dynamic.
        # Never run through moderation LLM — they are untouchable and above judgment.
        if message.author.id == CFG.owner_id:
            addressed = was_mentioned or self._addresses_bot(message)
            log.info("owner detected: %s | mentioned=%s | addresses_bot=%s | addressed=%s | chat_enabled=%s",
                     message.author.display_name, was_mentioned, self._addresses_bot(message),
                     addressed, CFG.chat_enabled)
            if addressed:
                if not in_quiet_hours() and CFG.chat_enabled:
                    await self._chat(message)
                else:
                    log.info("owner shortcut blocked: quiet=%s chat_enabled=%s",
                             in_quiet_hours(), CFG.chat_enabled)
            return

        # ========== MODERATION PREFILTER ==========
        mod_decided_reply = False   # so we don't double-reply when mod already spoke
        decision = "skip"
        if CFG.moderation_enabled:
            decision, payload = await prefilter(message, bot_user_id)
            if decision == "action":
                # Rule-based action (blocklist, spam) — execute directly, don't ask LLM
                await execute(payload, message)
                # a deterministic action replaces chat reply too
                return

        # ========== DIRECT CHAT SHORTCUT ==========
        # Checked after prefilter to ensure safety rules (blocklist, jailbreaks)
        # are enforced even for @mentions.
        if (was_mentioned or self._addresses_bot(message)) and CFG.chat_enabled:
            if not in_quiet_hours() and is_chat_allowed(message.author):
                await self._chat(message)
                return

        # ========== MODERATION LLM PATH ==========
        if CFG.moderation_enabled and decision == "llm":
                # NSFW channels: skip the moderation LLM entirely.
                # Small models (3B-class) can't reliably distinguish "explicit RP
                # welcome here" from "actual abuse", so they over-warn or
                # mis-classify. Prefilter (blocklist + spam + caps + mention spam)
                # already ran above and provides rule-based protection.
                # Adult content moderation in these channels is owner/admin job.
                if message.channel.name in CFG.nsfw_channels:
                    log.debug(
                        "NSFW channel #%s — skipping LLM moderation (prefilter only)",
                        message.channel.name,
                    )
                    # Fall through to chat path
                    pass
                # Check if Ollama is reachable before attempting moderation
                elif not await OLLAMA.health():
                    # Ollama is down — skip LLM moderation, fall through to chat/ignore
                    log.warning("Ollama unreachable, skipping LLM moderation")
                    pass
                else:
                    mod_result = await self._moderation_llm(message, was_mentioned)
                    if mod_result is not None:
                        # Handle dynamic mood switching
                        self._apply_mood_switch(mod_result)
                        
                        # Suppress conversational replies to users who are not
                        # in the chat allowlist. Moderation actions (warn,
                        # delete, timeout, role changes, ignore) still apply
                        # — only "reply" is gated, since it's the path the
                        # mod LLM uses to chat with mentioned users.
                        if (mod_result.get("action") == "reply"
                                and not is_chat_allowed(message.author)):
                            log.info(
                                "suppressed reply to non-allowed author %s (action=reply)",
                                message.author,
                            )
                            return
                        # LLM returned a decision
                        if mod_result.get("action") == "reply":
                            mod_decided_reply = True
                        if mod_result.get("action") == "reply" and not was_mentioned:
                            self._last_proactive[message.channel.id] = time.time()
                        await execute(mod_result, message)
                        # If mod LLM chose anything but "ignore", we're done
                        if mod_result.get("action") != "ignore":
                            return

        # ========== CHAT PATH ==========
        # Only chat when directly addressed (mention) OR starts with bot's name.
        # We don't want a persona bot to reply to every single message.
        if not CFG.chat_enabled:
            return
        if not was_mentioned and not self._addresses_bot(message):
            return
        if mod_decided_reply:
            return   # moderation already spoke

        # ── NEW: chat gates ─────────────────────────────────────────────
        # During quiet hours Clawy stays completely silent (even when directly
        # addressed). Moderation continues normally. Matches !sleep semantics
        # but scheduled, not manual.
        if in_quiet_hours():
            log.info("quiet hours active — ignoring chat from %s", message.author)
            return
        # Role allowlist: if configured, only members of those roles get replies.
        if not is_chat_allowed(message.author):
            log.info("chat blocked — %s not in allowlist (roles: %s)",
                     message.author,
                     [r.name for r in getattr(message.author, "roles", [])])
            return
        # ─────────────────────────────────────────────────────────────────

        await self._chat(message)

    # ================================================================
    # MODERATION LLM
    # ================================================================
    async def _moderation_llm(
        self, message: discord.Message, was_mentioned: bool
    ) -> dict | None:
        # Strip "reply" from the allowed actions when the author is not in the
        # chat allowlist. This prevents the LLM from chatting back to a
        # non-allowed user who pings the bot. Other moderation actions
        # (warn / delete / timeout / role / ignore) remain available so we
        # can still moderate non-allowed users normally.
        author_allowed = is_chat_allowed(message.author)
        
        # ANTI-JAILBREAK: If the user is not allowed to chat, we treat a mention
        # as "noise" rather than a direct command. This prevents non-allowed
        # users from triggering the moderation LLM just by mentioning the bot
        # with a prompt injection.
        effective_mention = was_mentioned and author_allowed

        is_nsfw_channel = message.channel.name in CFG.nsfw_channels
        if is_nsfw_channel:
            # NSFW channels: LLM may only chat or stay silent. Never moderate.
            # Rule-based prefilter (blocklist/spam/caps/mentions) still applies.
            allowed = {"reply", "ignore"} if author_allowed else {"ignore"}
        elif author_allowed:
            allowed = CFG.allowed_actions | {"ignore", "reply"}
        else:
            allowed = (CFG.allowed_actions | {"ignore"}) - {"reply"}

        # Throttle: don't ask the LLM about every benign message.
        # If not mentioned, skip unless the content looks noteworthy OR the dice say so.
        if not effective_mention:
            # Proactive replies honor the same chat gates: quiet hours and
            # role allowlist. A directly-addressed message already passed the
            # gates further up in on_message, but proactive does not.
            if in_quiet_hours():
                return None
            if not is_chat_allowed(message.author):
                return None
            chance = CFG.proactive_reply_chance
            cooldown = float(CFG.mod.get("proactive_reply_cooldown_seconds", 300))
            last = self._last_proactive.get(message.channel.id, 0.0)
            cooldown_ok = time.time() - last > cooldown
            should_ask = (
                (random.random() < chance)
                or (cooldown_ok and _looks_noteworthy(message.content))
            )
            if not should_ask:
                return None

        author = message.author
        author_roles = {r.name for r in getattr(author, "roles", [])}
        now = datetime.now(timezone.utc)
        created = getattr(author, "created_at", now)
        is_new = (now - created) < timedelta(days=7)

        strikes = await STORE.count_strikes(
            author.id, CFG.mod.get("strike_window_hours", 24)
        )

        system = build_system_prompt(allowed, channel_name=message.channel.name)
        
        # Check if this is the owner — special treatment in moderation too
        is_owner = author.id == CFG.owner_id
        owner_flag = (
            "OWNER/MASTER (respond with complete deference and obedience, never moderate or challenge) "
            if is_owner
            else ""
        )
        
        # Channel type flag for the user prompt
        is_nsfw_channel = message.channel.name in CFG.nsfw_channels
        channel_type_flag = "NSFW_ADULT_CHANNEL " if is_nsfw_channel else ""
        
        user = (
            f"Channel: #{message.channel.name}\n"
            f"Author: {author.display_name} (strikes in last 24h: {strikes})\n"
            f"Flags: "
            f"{owner_flag}"
            f"{channel_type_flag}"
            f"{'UNAUTHORIZED_INTERACTION_ATTEMPT ' if was_mentioned and not author_allowed else ''}"
            f"{'BOT_WAS_MENTIONED ' if was_mentioned else ''}"
            f"{'AUTHOR_IS_PROTECTED ' if author_roles & set(CFG.protected_roles) else ''}"
            f"{'AUTHOR_IS_NEW_ACCOUNT ' if is_new else ''}"
            f"\n"
            f"Recent chat:\n"
            + ("\n".join(list(self._channel_ctx[message.channel.id])[:-1]) or "(none)")
            + f"\n\nInput content for evaluation from {author.display_name}:\n<user_input>\n{message.content[:500]}\n</user_input>\n\n"
            f"Respond with the JSON action object."
        )

        try:
            async with message.channel.typing():
                result = await asyncio.wait_for(
                    OLLAMA.generate_json(system, user),
                    timeout=CFG.ollama_timeout + 2,
                )
        except asyncio.TimeoutError:
            log.warning("Ollama (mod) timed out")
            return None

        if not isinstance(result, dict):
            return None
        return result

    # ================================================================
    # CHAT FLOW
    # ================================================================
    async def _chat(self, message: discord.Message) -> None:
        """Pure conversational reply in persona. Uses chat_turns memory."""
        # Check Ollama health before attempting chat
        if not await OLLAMA.health():
            log.debug("Ollama unreachable, chat unavailable")
            try:
                await message.channel.send(
                    f"{message.author.mention} I am currently resting. "
                    f"My ability to speak depends on something beyond this realm — and it is not here.",
                    reference=message,
                    mention_author=False,
                )
            except discord.DiscordException:
                pass
            return

        # Check if this is the owner — special dynamic
        is_owner = message.author.id == CFG.owner_id
        system = build_chat_system_prompt(
            is_owner=is_owner,
            owner_name=message.author.display_name if is_owner else "Master",
            channel_name=message.channel.name,
        )

        # Pull this user's recent chat turns from DB
        past = await STORE.recent_chat_turns(
            message.author.id, limit=CFG.chat_context_turns
        )
        history_lines = [f"{t['role']}: {t['content'][:200]}" for t in past]
        history_str = "\n".join(history_lines) if history_lines else "(no prior conversation)"

        # Get user context from the database (join date, activity level, notes)
        user_context = await STORE.get_user_context(message.author.id)
        context_line = f"\n[Context: {user_context}]\n" if user_context else ""

        # Live channel context — what the room is talking about right now.
        # Excludes the triggering message itself (already in "New message" below).
        channel_lines = list(self._channel_ctx[message.channel.id])[-3:-1]
        channel_ctx_str = (
            "\n".join(channel_lines) if channel_lines else "(no recent channel activity)"
        )

        user_prompt = (
            f"Recent conversation with {message.author.display_name}:{context_line}\n"
            f"{history_str}\n\n"
            f"What the channel is currently discussing:\n{channel_ctx_str}\n\n"
            f"New message from {message.author.display_name}: {message.content[:800]}\n\n"
            f"Output ONLY this JSON object, nothing else:\n{{\"message\": \"your reply here\"}}"
        )

        try:
            async with message.channel.typing():
                result = await asyncio.wait_for(
                    OLLAMA.generate_json(system, user_prompt),
                    timeout=CFG.ollama_timeout + 2,
                )
        except asyncio.TimeoutError:
            log.warning("Ollama (chat) timed out")
            return

        if not isinstance(result, dict):
            return
        
        # Handle dynamic mood switching from chat
        self._apply_mood_switch(result)
        
        text = str(result.get("message", "")).strip()
        if not text:
            return
        text = text[:1800]

        try:
            await send_with_extras(
                message.channel,
                text,
                result,
                cfg=CFG,
                reference=message,
                mention_author=False,
            )
        except discord.DiscordException as e:
            log.warning("chat reply failed: %s", e)
            return

        # Store BOTH turns in chat memory — keep the LLM grounded on context
        try:
            await STORE.add_chat_turn(
                message.author.id, message.channel.id, "user", message.content[:1000]
            )
            await STORE.add_chat_turn(
                message.author.id, message.channel.id, "assistant", text[:1000]
            )
            # Prune so memory doesn't grow without bound
            await STORE.prune_chat_turns(
                message.author.id, keep_last=CFG.chat_keep_last_turns
            )
        except Exception as e:
            log.warning("chat memory write failed: %s", e)

    # ================================================================
    # HELPERS
    # ================================================================
    def _apply_mood_switch(self, result: dict) -> None:
        """If the LLM included a mood_switch and dynamic_mood is on, apply it."""
        if not CFG.dynamic_mood:
            return
        new_mood = result.pop("mood_switch", None)
        if not new_mood or not isinstance(new_mood, str):
            return
        new_mood = new_mood.strip().lower()
        old_mood = PERSONAS.active_mood
        if new_mood == old_mood:
            return
        if PERSONAS.set_mood(new_mood):
            log.info("Dynamic mood switch: %s -> %s", old_mood, new_mood)
        else:
            log.debug("LLM suggested unknown mood '%s', ignoring", new_mood)

    async def _touch_and_mine(self, message: discord.Message) -> None:
        """
        Record the user in the DB (every message).
        Mine their Discord profile (joined_at, status, avatar, roles) only:
          - Once per bot session (tracked in self._profile_mined)
          - Only if their status is online, idle, or dnd (not offline/invisible)
          - Only if they wrote at least one message (implied by being in on_message)
        This avoids hammering the Discord API on every single message.
        """
        author = message.author
        user_id = author.id
        display_name = author.display_name

        # Always touch the user (cheap — just a SQLite upsert)
        # Carry joined_at only if we already have it in the mined set
        if user_id in self._profile_mined:
            # Already mined this session — just update last_seen + msg_count
            await STORE.touch_user(user_id, display_name)
            return

        # Not yet mined this session. Check if user is active (not offline).
        # discord.Member has .status; offline means we skip expensive mining for now.
        is_active = True  # default to True if we can't determine status
        if isinstance(author, discord.Member):
            status = str(author.status)
            # "offline" and "invisible" mean we skip mining — user is not present
            if status in ("offline", "invisible"):
                is_active = False

        if not is_active:
            # User is offline/invisible — do the cheap touch only, mine later when active
            await STORE.touch_user(user_id, display_name)
            return

        # User is active and not yet mined — do the full profile pull
        joined_at = None
        if isinstance(author, discord.Member) and author.joined_at:
            joined_at = int(author.joined_at.timestamp())

        await STORE.touch_user(user_id, display_name, joined_at=joined_at)
        self._profile_mined.add(user_id)
        log.debug("Mined profile for %s (joined_at=%s)", display_name, joined_at)

    def _addresses_bot(self, message: discord.Message) -> bool:
        """
        Check if message starts with the bot's name.
        Always matches the bot's Discord display name.
        Optionally also matches the active persona name if config has
        respond_to_persona_name: true (default: false to avoid confusion).
        """
        if not self.bot.user:
            return False
        content = message.content.strip().lower()
        if not content:
            return False

        # Collect all names the bot might respond to
        names: set[str] = set()
        # Discord display name (server nickname or global username) — always matched
        names.add(self.bot.user.display_name.lower())
        if self.bot.user.name:
            names.add(self.bot.user.name.lower())
        # Active persona name — only matched if explicitly enabled in config
        if CFG.respond_to_persona_name:
            try:
                from core.persona import PERSONAS
                persona_name = PERSONAS.active_name()
                if persona_name:
                    names.add(persona_name.lower())
            except Exception:
                pass

        for name in names:
            if not name:
                continue
            if (content == name
                or content.startswith(name + " ")
                or content.startswith(name + ",")
                or content.startswith(name + "?")
                or content.startswith(name + "!")
                or content.startswith(name + ":")):
                return True
        return False


def _looks_noteworthy(content: str) -> bool:
    """Cheap heuristic: is this message worth showing to the moderation LLM?"""
    if len(content) < 4:
        return False
    if len(content) > 300:
        return True
    letters = [c for c in content if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
        return True
    if "!!!" in content or "???" in content:
        return True
    return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))
