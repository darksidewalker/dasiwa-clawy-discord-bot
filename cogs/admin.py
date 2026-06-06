"""Owner/admin-only runtime controls."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import discord
from discord.ext import commands

from core.config import CFG, VALID_MODES
from core.executor import execute_ban, execute_kick, execute_mute
from core.expressions import EXPRESSIONS, send_with_extras
from core.gating import in_quiet_hours, quiet_status_line
from core.triggers import TRIGGERS
from core.ollama_client import OLLAMA
from core.persona import PERSONAS
from core.prefilter import BLOCKLIST, _blocklist_enabled, _blocklist_path
from core.store import STORE

from ._common import CleanCommandCog, ack, reply_permanent

log = logging.getLogger(__name__)


def _reload_role_rules() -> str:
    """Reload the role engine; returns a status string."""
    try:
        from cogs.roles import RULE_ENGINE
        n = RULE_ENGINE.reload()
        return f"role_rules.json ({n} rules)"
    except Exception as e:
        return f"role_rules.json (FAILED: {e})"


def _is_admin(ctx: commands.Context) -> bool:
    if ctx.author.id == CFG.owner_id:
        return True
    if isinstance(ctx.author, discord.Member):
        return ctx.author.guild_permissions.administrator
    return False


class AdminCog(CleanCommandCog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def is_authorized(self, ctx: commands.Context) -> bool:
        return _is_admin(ctx)

    # ---------- help ----------
    @commands.command(name="help")
    async def help_cmd(self, ctx: commands.Context, which: str = "") -> None:
        """List available commands, or show details for one.

        Usage:
          !help            list all commands grouped by function
          !help <command>  show the detail/docstring for one command
        """
        if which:
            # Detail view: strip a leading '!' if present, find the command.
            name = which.lstrip("!").strip().lower()
            cmd = self.bot.get_command(name)
            if cmd is None:
                await ack(ctx, f"Unknown command `{name}`.")
                return
            doc = (cmd.help or cmd.short_doc or "(no description)").strip()
            sig = cmd.signature or ""
            lines = [
                f"**!{cmd.name}** {sig}".rstrip(),
                f"```",
                doc,
                f"```",
            ]
            if cmd.aliases:
                lines.append(f"Aliases: {', '.join('`!' + a + '`' for a in cmd.aliases)}")
            await reply_permanent(ctx, "\n".join(lines))
            return

        # List view: grouped. Keep short — detail via !help <command>.
        groups: list[tuple[str, list[tuple[str, str]]]] = [
            ("Kill switch", [
                ("pause",   "stop all autonomous actions"),
                ("resume",  "re-enable autonomous actions"),
                ("reload",  "hot-reload all configs + clear overrides"),
                ("sleep",   "silence Clawy (optionally for a duration)"),
                ("wake",    "wake Clawy from sleep"),
                ("sleepstatus", "show sleep state"),
            ]),
            ("Mode & persona", [
                ("mode",    "show/switch bot mode"),
                ("persona", "show/switch persona (or reload)"),
                ("mood",    "show/switch mood for active persona"),
                ("dynmood", "toggle LLM autonomous mood switching"),
                ("expressions", "show/reload emoji + media pool"),
                ("triggers", "show/reload keyword→media triggers"),
                ("model",   "show/switch Ollama model (session)"),
                ("think",   "toggle Ollama reasoning trace on/off"),
            ]),
            ("Chat gating", [
                ("quiet",      "scheduled quiet hours — Clawy silent"),
                ("chatroles",  "role allowlist — who Clawy chats with"),
                ("proactive",  "chance of unsolicited replies"),
                ("jumpin",     "make Clawy jump into the last N channel messages"),
                ("nsfw",       "manage NSFW/adult channel list"),
            ]),
            ("Moderation", [
                ("kick",   "manually kick a member"),
                ("ban",    "manually ban a member"),
                ("mute",   "manually timeout a member"),
                ("unmute", "remove a timeout"),
            ]),
            ("User info / memory", [
                ("whois",   "DB profile for a user"),
                ("strikes", "strike count + recent mod events"),
                ("recall",  "show chat memory with a user"),
                ("forget",  "wipe a user's chat memory"),
            ]),
            ("Message moving", [
                ("moveto",   "move replied message (+ N) to a channel"),
                ("movelast", "move a user's last N messages to a channel"),
            ]),
            ("Message purging", [
                ("purgethis", "delete a single replied-to message (always notifies)"),
                ("purge",     "delete last N messages in a channel (optional @user filter)"),
                ("purgeuser", "delete last N messages from a user in a channel"),
            ]),
            ("Roles engine", [
                ("roles",    "manage activity-based role rules"),
            ]),
            ("Diagnostics", [
                ("diag",     "health check across all subsystems (`!diag verbose` for full)"),
                ("perms",    "show Clawy's permissions in this channel"),
                ("setlog",   "set the log channel (session)"),
                ("help",     "this list (use `!help <command>` for details)"),
            ]),
        ]

        lines = ["**Clawy's commands** — `!help <command>` for details"]
        for title, cmds in groups:
            lines.append("")
            lines.append(f"__{title}__")
            for name, blurb in cmds:
                lines.append(f"  `!{name}` — {blurb}")
        await reply_permanent(ctx, "\n".join(lines))

    # ---------- kill switch ----------
    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context) -> None:
        CFG.state.paused = True
        await ack(ctx, "Paused. No autonomous actions until `!resume`.")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context) -> None:
        CFG.state.paused = False
        await ack(ctx, "Resumed.")

    # ---------- full reload ----------
    @commands.command(name="reload")
    async def reload_cmd(self, ctx: commands.Context) -> None:
        """Hot-reload ALL configs from disk and reset session overrides.

        Reloads: config.yaml, personas.json, role_rules.json, blocklist.
        Clears: all session overrides (mode, model, think, quiet hours,
        chat roles, proactive chance). Does NOT restart the bot process
        or reconnect to Discord — it's a config-level fresh start.

        Usage: !reload
        """
        reloaded = []

        # 1. config.yaml
        try:
            CFG.reload_yaml()
            reloaded.append("config.yaml")
        except Exception as e:
            log.warning("reload config.yaml failed: %s", e)
            await ack(ctx, f"config.yaml reload failed: `{e}`")
            return

        # 2. personas.json
        try:
            PERSONAS.reload()
            reloaded.append("personas.json")
        except Exception as e:
            log.warning("reload personas.json failed: %s", e)
            reloaded.append(f"personas.json (FAILED: {e})")

        # 3. emoji_mapping.json + media_pool.json
        try:
            ne, nm = EXPRESSIONS.reload()
            reloaded.append(f"expressions ({ne} emoji, {nm} media)")
        except Exception as e:
            log.warning("reload expressions failed: %s", e)
            reloaded.append(f"expressions (FAILED: {e})")

        # 4. triggers.json
        try:
            nt = TRIGGERS.reload()
            reloaded.append(f"triggers ({nt} loaded)")
        except Exception as e:
            log.warning("reload triggers failed: %s", e)
            reloaded.append(f"triggers (FAILED: {e})")

        # 5. role_rules.json
        reloaded.append(_reload_role_rules())

        # 6. blocklist
        if _blocklist_enabled():
            try:
                n = BLOCKLIST.reload(_blocklist_path())
                reloaded.append(f"blocklist.json ({n} entries)")
            except Exception as e:
                reloaded.append(f"blocklist.json (FAILED: {e})")
        else:
            reloaded.append("blocklist (disabled)")

        # 7. Reset ALL session overrides to force YAML values
        CFG.state.mode_override = None
        CFG.state.model_override = None
        CFG.state.think_override = None
        CFG.state.quiet_hours_enabled_override = None
        CFG.state.quiet_hours_start_override = None
        CFG.state.quiet_hours_end_override = None
        CFG.state.quiet_hours_timezone_override = None
        CFG.state.chat_allowed_roles_override = None
        CFG.state.proactive_chance_override = None
        CFG.state.paused = False
        # Note: sleeping state is NOT reset — use !wake for that.

        summary = " | ".join(reloaded)
        await ack(ctx, f"Reloaded: {summary}. All session overrides cleared.")

    # ---------- mode ----------
    @commands.command(name="mode")
    async def mode(self, ctx: commands.Context, new_mode: str = "") -> None:
        if not new_mode:
            await reply_permanent(ctx,
                f"Current mode: `{CFG.mode}`\n"
                f"Options: {', '.join(VALID_MODES)}"
            )
            return
        if new_mode not in VALID_MODES:
            await ack(ctx, f"Unknown mode. Options: {', '.join(VALID_MODES)}")
            return
        CFG.state.mode_override = new_mode  # type: ignore[assignment]
        await ack(ctx, f"Mode set to `{new_mode}` (session-only, until restart).")

    # ---------- persona & mood ----------
    @commands.command(name="persona")
    async def persona(self, ctx: commands.Context, key: str = "") -> None:
        if not key:
            lines = [f"Active: **{PERSONAS.active_key}** / mood **{PERSONAS.active_mood}**", ""]
            for k in PERSONAS.list_personas():
                lines.append(PERSONAS.describe(k))
                lines.append("")
            await reply_permanent(ctx, "\n".join(lines)[:1900])
            return
        if key == "reload":
            PERSONAS.reload()
            await ack(ctx, "Personas reloaded from disk.")
            return
        if PERSONAS.set_persona(key):
            await ack(ctx, f"Persona set to **{key}** (mood: {PERSONAS.active_mood}).")
        else:
            await ack(ctx, 
                f"Unknown persona `{key}`. Available: {', '.join(PERSONAS.list_personas())}"
            )

    @commands.command(name="mood")
    async def mood(self, ctx: commands.Context, mood_name: str = "") -> None:
        if not mood_name:
            moods = PERSONAS.list_moods()
            await reply_permanent(ctx,
                f"Active mood: **{PERSONAS.active_mood}**\n"
                f"Available for `{PERSONAS.active_key}`: {', '.join(moods)}"
            )
            return
        if PERSONAS.set_mood(mood_name):
            await ack(ctx, f"Mood set to **{mood_name}**.")
        else:
            await ack(ctx, 
                f"Unknown mood `{mood_name}`. Available: {', '.join(PERSONAS.list_moods())}"
            )

    # ---------- expressions (emoji + media pool) ----------
    @commands.command(name="expressions")
    async def expressions(self, ctx: commands.Context, sub: str = "") -> None:
        """Show the loaded emoji mapping and media pool, or reload them.

        Usage:
          !expressions          — list emoji and media keys with descriptions
          !expressions reload   — reload emoji_mapping.json and media_pool.json
        """
        if sub == "reload":
            try:
                ne, nm = EXPRESSIONS.reload()
            except Exception as e:
                log.warning("expressions reload failed: %s", e)
                await ack(ctx, f"Reload failed: `{e}`")
                return
            await ack(ctx, f"Expressions reloaded: {ne} emoji, {nm} media items.")
            return

        # No subcommand — show what's loaded.
        emoji_names = EXPRESSIONS.emoji_names()
        media_keys = EXPRESSIONS.media_keys()

        lines = [
            f"**Expressions**  ·  enabled: `{CFG.expressions_enabled}`",
            (
                f"react: `{CFG.expressions_allow_reactions}`  ·  "
                f"sticker: `{CFG.expressions_allow_stickers}`  ·  "
                f"attach: `{CFG.expressions_allow_attachments}`  ·  "
                f"prompt_limit: `{CFG.expressions_prompt_limit}`  ·  "
                f"max_reactions: `{CFG.expressions_max_reactions}`"
            ),
            "",
            f"__Emoji ({len(emoji_names)})__",
        ]
        if emoji_names:
            for name in emoji_names[:40]:
                desc = EXPRESSIONS.emoji_description(name) or ""
                lines.append(f"  `{name}` — {desc[:120]}")
            if len(emoji_names) > 40:
                lines.append(f"  …and {len(emoji_names) - 40} more")
        else:
            lines.append("  (none — edit `config/emoji_mapping.json`)")

        lines.append("")
        lines.append(f"__Media pool ({len(media_keys)})__")
        if media_keys:
            for key in media_keys[:40]:
                entry = EXPRESSIONS.media_entry(key) or {}
                t = entry.get("type", "?")
                desc = entry.get("description", "")
                lines.append(f"  `{key}` ({t}) — {desc[:120]}")
            if len(media_keys) > 40:
                lines.append(f"  …and {len(media_keys) - 40} more")
        else:
            lines.append("  (none — edit `config/media_pool.json`)")

        await reply_permanent(ctx, "\n".join(lines)[:1900])

    # ---------- triggers (deterministic media reflex) ----------
    @commands.command(name="triggers")
    async def triggers(self, ctx: commands.Context, sub: str = "") -> None:
        """Show loaded triggers or reload them from config/triggers.json.

        Usage:
          !triggers          — list all loaded triggers with patterns and cooldowns
          !triggers reload   — reload triggers.json from disk (preserves cooldown state)
        """
        if sub == "reload":
            try:
                n = TRIGGERS.reload()
            except Exception as e:
                log.warning("triggers reload failed: %s", e)
                await ack(ctx, f"Reload failed: `{e}`")
                return
            await ack(ctx, f"Triggers reloaded: {n} loaded.")
            return

        # No subcommand — show what's loaded.
        all_triggers = TRIGGERS.list_triggers()
        lines = [
            f"**Triggers**  ·  enabled: `{CFG.triggers_enabled}`  ·  "
            f"max per message: `{CFG.triggers_max_per_message}`",
            "",
        ]
        if not all_triggers:
            lines.append("(none loaded — edit `config/triggers.json`)")
        else:
            for trig in all_triggers:
                ch_id = ctx.channel.id
                cd_remaining = TRIGGERS.cooldown_remaining(trig.name, ch_id)
                cd_state = (
                    f"⏳{cd_remaining}s here" if cd_remaining > 0
                    else "ready here"
                )
                lines.append(
                    f"`{trig.name}` ({trig.type})  ·  cooldown {trig.cooldown_seconds}s  "
                    f"·  {cd_state}"
                )
                lines.append(f"  patterns: {trig.pattern_summary()}")
                lines.append(f"  media: {', '.join(f'`{m}`' for m in trig.media)}")
                if trig.description:
                    lines.append(f"  — {trig.description}")
                lines.append("")

        await reply_permanent(ctx, "\n".join(lines)[:1900])

    # ---------- model ----------
    @commands.command(name="model")
    async def model(self, ctx: commands.Context, name: str = "") -> None:
        if not name:
            await reply_permanent(ctx, f"Current model: `{CFG.model}`")
            return
        CFG.state.model_override = name
        await ack(ctx, f"Model set to `{name}` (session-only).")

    # ---------- thinking toggle ----------
    @commands.command(name="think")
    async def think(self, ctx: commands.Context, arg: str = "") -> None:
        """Toggle Ollama's reasoning phase. Usage: !think | !think on | !think off"""
        if not arg:
            yaml_default = bool(CFG.raw.get("ollama", {}).get("think", False))
            override = CFG.state.think_override
            src = "override" if override is not None else "config"
            await reply_permanent(ctx,
                f"Thinking: **{'on' if CFG.think else 'off'}** (source: {src})\n"
                f"YAML default: `{yaml_default}`.  Usage: `!think on` / `!think off` / `!think reset`"
            )
            return
        a = arg.strip().lower()
        if a in ("on", "true", "1", "yes", "enable"):
            CFG.state.think_override = True
        elif a in ("off", "false", "0", "no", "disable"):
            CFG.state.think_override = False
        elif a in ("reset", "default", "clear"):
            CFG.state.think_override = None
        else:
            await ack(ctx, "Use `!think on`, `!think off`, or `!think reset`.")
            return
        await ack(ctx, f"Thinking is now **{'on' if CFG.think else 'off'}** (session-only).")

    # ---------- log channel ----------
    @commands.command(name="setlog")
    async def setlog(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        if channel is None:
            await ack(ctx, "Usage: `!setlog #channel`")
            return
        CFG.raw["log_channel_id"] = channel.id
        await ack(ctx, f"Log channel set to {channel.mention} (until restart).")

    # ---------- health ----------
    @commands.command(name="diag")
    async def diag(self, ctx: commands.Context, verbose: str = "") -> None:
        """Health check across all subsystems.

        Usage:
          !diag           — concise summary (one screen)
          !diag verbose   — adds full catalog listings (emoji names, media keys, etc.)
        """
        is_verbose = verbose.lower() in {"verbose", "v", "full", "all"}
        lines: list[str] = []

        # ─── 1. Health ───────────────────────────────────────────────
        t0 = time.time()
        healthy = await OLLAMA.health()
        latency_ms = int((time.time() - t0) * 1000)
        lines.append("__**Health**__")
        lines.append(
            f"  Ollama: {'✅' if healthy else '❌'} `{CFG.ollama_url}` "
            f"({latency_ms}ms)  ·  model `{CFG.model}`  ·  think {'on' if CFG.think else 'off'}"
        )
        bot_user = self.bot.user
        guild_name = ctx.guild.name if ctx.guild else "(no guild)"
        lines.append(
            f"  Discord: ✅ `{bot_user}` in **{guild_name}**"
            if bot_user else "  Discord: ❌ no user (not connected?)"
        )
        # Log channel reachability
        log_ch_status = "not configured"
        if CFG.log_channel_id and ctx.guild:
            log_ch = ctx.guild.get_channel(CFG.log_channel_id)
            if log_ch is None:
                log_ch_status = f"❌ id `{CFG.log_channel_id}` not found in this guild"
            elif not isinstance(log_ch, discord.TextChannel):
                log_ch_status = f"❌ id `{CFG.log_channel_id}` is not a text channel"
            else:
                me = ctx.guild.me
                can_send = me is not None and log_ch.permissions_for(me).send_messages
                log_ch_status = (
                    f"{'✅' if can_send else '❌'} {log_ch.mention}"
                    + ("" if can_send else " (no send permission)")
                )
        lines.append(f"  Log channel: {log_ch_status}")
        # Database
        try:
            db_path = Path(CFG.db_path)
            if db_path.exists():
                size_mb = db_path.stat().st_size / (1024 * 1024)
                lines.append(f"  Database: ✅ `{CFG.db_path}` ({size_mb:.1f} MB)")
            else:
                lines.append(f"  Database: ⚠️ `{CFG.db_path}` (will be created on first write)")
        except Exception as e:
            lines.append(f"  Database: ❌ `{CFG.db_path}` ({e})")

        # ─── 2. Identity & state ─────────────────────────────────────
        lines.append("")
        lines.append("__**Identity & state**__")
        you_ok = "✅ match" if ctx.author.id == CFG.owner_id else "❌ NOT owner"
        lines.append(f"  Owner: `{CFG.owner_id}`  ·  you `{ctx.author.id}` ({you_ok})")
        lines.append(
            f"  Persona: `{PERSONAS.active_key}` / mood `{PERSONAS.active_mood}`  "
            f"·  dynamic mood {'on' if CFG.dynamic_mood else 'off'}"
        )
        sleeping = getattr(CFG.state, "sleeping", False)
        lines.append(
            f"  Mode: `{CFG.mode}`  ·  paused {'YES' if CFG.state.paused else 'no'}  "
            f"·  sleeping {'YES' if sleeping else 'no'}"
        )

        # ─── 3. Moderation ───────────────────────────────────────────
        lines.append("")
        lines.append("__**Moderation**__")
        lines.append(f"  Allowed actions: {', '.join(sorted(CFG.allowed_actions)) or '(none)'}")
        strike_window = CFG.mod.get("strike_window_hours", 24)
        lines.append(
            f"  Strike window: {strike_window}h  ·  autonomous timeout cap: "
            f"{CFG.max_autonomous_timeout_seconds}s"
        )
        # Blocklist
        if _blocklist_enabled():
            try:
                n_words = len(BLOCKLIST._words)
                n_phrases = len(BLOCKLIST._phrases)
                lines.append(
                    f"  Blocklist: ✅ enabled  ·  {n_words} word(s), {n_phrases} phrase(s)"
                )
            except Exception as e:
                lines.append(f"  Blocklist: ⚠️ enabled but read failed: `{e}`")
        else:
            lines.append("  Blocklist: disabled")
        # Spam prefilter
        spam_n = CFG.mod.get("spam_threshold", 6)
        spam_w = CFG.mod.get("spam_window_seconds", 10)
        spam_strikes = CFG.mod.get("spam_strike_threshold", 3)
        lines.append(
            f"  Spam prefilter: {spam_n} msgs / {spam_w}s  ·  "
            f"strike threshold {spam_strikes}"
        )
        # Role engine
        try:
            from cogs.roles import RULE_ENGINE
            n_rules = len(RULE_ENGINE.rules())
            lines.append(f"  Role rules: {n_rules} loaded")
        except Exception as e:
            lines.append(f"  Role rules: ⚠️ engine read failed: `{e}`")
        # Protected roles
        prot = ", ".join(CFG.protected_roles) if CFG.protected_roles else "(none)"
        lines.append(f"  Protected roles: {prot}")
        # NSFW channels
        nsfw = ", ".join(CFG.nsfw_channels) if CFG.nsfw_channels else "(none)"
        lines.append(f"  NSFW channels: {nsfw}")
        # Notify user
        if CFG.notify_user_enabled:
            parts = []
            if CFG.notify_user_dm:
                parts.append("DM")
            if CFG.notify_user_channel_notice:
                parts.append(f"{CFG.notify_user_notice_seconds}s channel notice")
            detail = " + ".join(parts) if parts else "(nothing — both subflags off)"
            lines.append(f"  Notify on delete/move/purge: ✅ {detail}")
        else:
            lines.append("  Notify on delete/move/purge: ❌ disabled")

        # ─── 4. Expressions ──────────────────────────────────────────
        lines.append("")
        lines.append("__**Expressions**__")
        if CFG.expressions_enabled:
            emoji_names = EXPRESSIONS.emoji_names()
            media_keys = EXPRESSIONS.media_keys()
            n_stickers = sum(
                1 for k in media_keys
                if (EXPRESSIONS.media_entry(k) or {}).get("type") == "sticker"
            )
            n_files = sum(
                1 for k in media_keys
                if (EXPRESSIONS.media_entry(k) or {}).get("type") == "file"
            )
            n_urls = sum(
                1 for k in media_keys
                if (EXPRESSIONS.media_entry(k) or {}).get("type") == "url"
            )
            flags = (
                f"react {'✓' if CFG.expressions_allow_reactions else '✗'}  "
                f"sticker {'✓' if CFG.expressions_allow_stickers else '✗'}  "
                f"attach {'✓' if CFG.expressions_allow_attachments else '✗'}"
            )
            lines.append(f"  Status: ✅ enabled  ·  {flags}")
            lines.append(
                f"  Pool: {len(emoji_names)} emoji, {len(media_keys)} media "
                f"({n_stickers} sticker / {n_files} file / {n_urls} url)"
            )
            lines.append(
                f"  Prompt limit: {CFG.expressions_prompt_limit}/category  ·  "
                f"reaction cap: {CFG.expressions_max_reactions}/msg"
            )
        else:
            lines.append("  Status: ❌ disabled (LLM is not told about emoji/media)")

        # ─── 4b. Triggers ───────────────────────────────────────────
        lines.append("")
        lines.append("__**Triggers**__")
        if CFG.triggers_enabled:
            n_triggers = TRIGGERS.count()
            lines.append(
                f"  Status: ✅ enabled  ·  {n_triggers} loaded  ·  "
                f"max {CFG.triggers_max_per_message}/message"
            )
        else:
            lines.append("  Status: ❌ disabled (no triggers will fire)")

        # ─── 5. Chat gating ──────────────────────────────────────────
        lines.append("")
        lines.append("__**Chat gating**__")
        chat_state = "enabled" if CFG.chat_enabled else "disabled (mode excludes chat)"
        lines.append(f"  Chat: {chat_state}")
        allowlist = (
            ", ".join(CFG.chat_allowed_roles) if CFG.chat_allowed_roles else "everyone"
        )
        lines.append(f"  Role allowlist: {allowlist}")
        lines.append(f"  Proactive chance: {CFG.proactive_reply_chance:.3f}")
        quiet_state = "off"
        if CFG.quiet_hours_enabled:
            active = "ACTIVE NOW" if in_quiet_hours() else "idle"
            quiet_state = (
                f"on ({active}) — {CFG.quiet_hours_start}–{CFG.quiet_hours_end} "
                f"{CFG.quiet_hours_timezone}"
            )
        lines.append(f"  Quiet hours: {quiet_state}")
        ignored = (
            ", ".join(CFG.ignored_channels) if CFG.ignored_channels else "(none)"
        )
        lines.append(f"  Ignored channels: {ignored}")

        # ─── 6. Permissions (this channel) ───────────────────────────
        # Only the perms that matter for the autonomous + expressive features.
        # For a full breakdown, refer to !perms.
        if ctx.guild:
            me = ctx.guild.me
            if me is not None:
                try:
                    p = ctx.channel.permissions_for(me)
                    lines.append("")
                    lines.append(f"__**Permissions in {ctx.channel.mention}**__")
                    checks = [
                        ("Send Messages",       p.send_messages,        True),
                        ("Read History",        p.read_message_history, True),
                        ("Manage Messages",     p.manage_messages,      True),
                        ("Manage Webhooks",     p.manage_webhooks,      True),
                        ("Moderate Members",    p.moderate_members,     True),
                        ("Add Reactions",       p.add_reactions,        CFG.expressions_allow_reactions),
                        ("Use External Emoji",  p.use_external_emojis,  CFG.expressions_allow_reactions),
                        ("Use External Stickers", p.use_external_stickers, CFG.expressions_allow_stickers),
                        ("Attach Files",        p.attach_files,         CFG.expressions_allow_attachments),
                        ("Embed Links",         p.embed_links,          False),  # nice-to-have
                    ]
                    for name, ok, required in checks:
                        if required and not ok:
                            mark = "❌"
                            tail = "  ← REQUIRED for an enabled feature"
                        elif ok:
                            mark = "✅"
                            tail = ""
                        else:
                            mark = "·"
                            tail = ""
                        lines.append(f"  {mark} {name}{tail}")
                    lines.append("  (run `!perms` for the full breakdown)")
                except Exception as e:
                    lines.append(f"  Permissions check failed: `{e}`")

        # ─── 7. Verbose extras ───────────────────────────────────────
        if is_verbose:
            lines.append("")
            lines.append("__**Catalogs (verbose)**__")
            # Personas
            lines.append(f"  Personas ({len(PERSONAS.list_personas())}):")
            for k in PERSONAS.list_personas():
                moods = ", ".join(PERSONAS.list_moods(k))
                marker = "→" if k == PERSONAS.active_key else " "
                lines.append(f"    {marker} `{k}` — moods: {moods}")
            # Emoji
            emoji_names = EXPRESSIONS.emoji_names()
            lines.append(f"  Emoji ({len(emoji_names)}): "
                         + (", ".join(f"`{n}`" for n in emoji_names) or "(none)"))
            # Media
            media_keys = EXPRESSIONS.media_keys()
            if media_keys:
                lines.append(f"  Media ({len(media_keys)}):")
                for k in media_keys:
                    entry = EXPRESSIONS.media_entry(k) or {}
                    lines.append(f"    `{k}` ({entry.get('type', '?')})")
            else:
                lines.append("  Media: (none)")
            # Triggers
            all_triggers = TRIGGERS.list_triggers()
            if all_triggers:
                lines.append(f"  Triggers ({len(all_triggers)}):")
                for trig in all_triggers:
                    lines.append(
                        f"    `{trig.name}` ({trig.type}) → {', '.join(trig.media)}  "
                        f"·  cooldown {trig.cooldown_seconds}s"
                    )
            else:
                lines.append("  Triggers: (none)")

        # Send in chunks if the output exceeds Discord's per-message limit.
        # We split on blank lines (section boundaries) to keep groups together.
        text = "\n".join(lines)
        if len(text) <= 1900:
            await reply_permanent(ctx, text)
            return

        # Multi-chunk: greedily pack sections under 1900 chars each.
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in lines:
            # Account for the newline that will rejoin them.
            add_len = len(line) + 1
            if current_len + add_len > 1850 and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += add_len
        if current:
            chunks.append("\n".join(current))

        for i, chunk in enumerate(chunks):
            header = f"(diag {i + 1}/{len(chunks)})\n" if len(chunks) > 1 else ""
            await reply_permanent(ctx, header + chunk)

    # ---------- user info from moderation store ----------
    @commands.command(name="strikes")
    async def strikes(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if member is None:
            await ack(ctx, "Usage: `!strikes @user`")
            return
        n = await STORE.count_strikes(member.id, CFG.mod.get("strike_window_hours", 24))
        events = await STORE.recent_mod_events(member.id, limit=5)
        lines = [f"**{member.display_name}**: {n} strike(s) in last 24h", ""]
        if events:
            lines.append("Recent events:")
            for e in events:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
                lines.append(f"  • [{ts}] {e['kind']} — {e['reason'] or '(no reason)'}")
        await reply_permanent(ctx, "\n".join(lines)[:1900])

    @commands.command(name="whois")
    async def whois(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if member is None:
            await ack(ctx, "Usage: `!whois @user`")
            return
        info = await STORE.get_user(member.id)
        if not info:
            first = last = "Never"
            msg_count = 0
            notes = "(none)"
        else:
            first = time.strftime("%Y-%m-%d", time.localtime(info["first_seen"]))
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(info["last_seen"]))
            msg_count = info['msg_count']
            notes = info['notes'] or "(none)"

        roles = [r.name for r in member.roles]
        await reply_permanent(ctx,
            f"**{member.display_name}** (`{member.id}`)\n"
            f"• First seen: {first}\n"
            f"• Last seen: {last}\n"
            f"• Messages observed: {msg_count}\n"
            f"• Roles detected: {', '.join(roles) if roles else 'None'}\n"
            f"• Notes: {notes}"
        )

    # ---------- chat memory controls ----------
    @commands.command(name="forget")
    async def forget(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Wipe chat memory for a user (does NOT touch moderation history)."""
        if member is None:
            await ack(ctx, "Usage: `!forget @user` — clears this user's chat memory only.")
            return
        await STORE.forget_user_chat(member.id)
        await ack(ctx, f"Cleared chat memory for **{member.display_name}**. Mod history kept.")

    @commands.command(name="recall")
    async def recall(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Show what the bot remembers about a user (chat-wise)."""
        if member is None:
            await ack(ctx, "Usage: `!recall @user`")
            return
        summary = await STORE.get_chat_summary(member.id)
        turns = await STORE.recent_chat_turns(member.id, limit=10)
        if not turns and not summary:
            await ack(ctx, f"No chat memory for **{member.display_name}**.")
            return
        lines = [f"**Chat memory with {member.display_name}**"]
        if summary and summary.get("summary"):
            ts = time.strftime("%m-%d %H:%M", time.localtime(summary["updated_at"]))
            lines.append(f"`[summary updated {ts}]` {summary['summary'][:650]}")
        if turns:
            lines.append(f"**Recent raw turns** (most recent {len(turns)}):")
        for t in turns:
            ts = time.strftime("%m-%d %H:%M", time.localtime(t["ts"]))
            lines.append(f"`[{ts}] {t['role']}:` {t['content'][:150]}")
        await reply_permanent(ctx, "\n".join(lines)[:1900])


    # ---------- manual moderation (kick / ban / mute) ----------
    @commands.command(name="kick")
    async def kick(self, ctx: commands.Context, member: discord.Member | None = None, *, reason: str = "No reason given.") -> None:
        """Manually kick a member. Usage: !kick @user [reason]"""
        if member is None:
            await ack(ctx, "Usage: `!kick @user [reason]`")
            return
        result = await execute_kick(ctx.guild, member, reason, actor_id=ctx.author.id)
        await ack(ctx, f"`{result}`")

    @commands.command(name="ban")
    async def ban(self, ctx: commands.Context, member: discord.Member | None = None, *, reason: str = "No reason given.") -> None:
        """Manually ban a member. Usage: !ban @user [reason]"""
        if member is None:
            await ack(ctx, "Usage: `!ban @user [reason]`")
            return
        result = await execute_ban(ctx.guild, member, reason, actor_id=ctx.author.id)
        await ack(ctx, f"`{result}`")

    @commands.command(name="mute")
    async def mute(self, ctx: commands.Context, member: discord.Member | None = None, duration: str = "10m", *, reason: str = "No reason given.") -> None:
        """Manually mute a member. Usage: !mute @user [duration] [reason]
        Duration examples: 30m  2h  1h30m  (default: 10m)"""
        if member is None:
            await ack(ctx, "Usage: `!mute @user [duration] [reason]`")
            return
        import re
        m = re.match(r'^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$', duration.strip().lower())
        if not m or not any(m.groups()):
            await ack(ctx, "Invalid duration. Examples: `30m` `2h` `1h30m`")
            return
        seconds = int(m.group(1) or 0)*3600 + int(m.group(2) or 0)*60 + int(m.group(3) or 0)
        if seconds <= 0:
            await ack(ctx, "Duration must be greater than zero.")
            return
        result = await execute_mute(ctx.guild, member, seconds, reason, actor_id=ctx.author.id)
        await ack(ctx, f"`{result}`")

    @commands.command(name="unmute")
    async def unmute(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Remove a timeout from a member. Usage: !unmute @user"""
        if member is None:
            await ack(ctx, "Usage: `!unmute @user`")
            return
        try:
            await member.timeout(None, reason=f"Unmuted by {ctx.author}")
            await ack(ctx, f"Removed timeout from **{member.display_name}**.")
        except discord.Forbidden:
            await ack(ctx, "Missing permission to remove timeout.")

    # ---------- chat gating ----------
    @commands.command(name="quiet")
    async def quiet_cmd(
        self, ctx: commands.Context, sub: str = "", start: str = "", end: str = "", tz: str = ""
    ) -> None:
        """Manage quiet hours (Clawy silent on chat during a time window).

        Usage:
          !quiet                                   show status
          !quiet on                                enable (use config values)
          !quiet off                               disable
          !quiet set 23:00 07:00 Europe/Berlin     set the window (session-only)
          !quiet reset                             drop session overrides, use YAML
        """
        if not sub:
            enabled = CFG.quiet_hours_enabled
            lines = [
                f"**Quiet hours:** {'ON' if enabled else 'OFF'}",
                f"**Window:** {CFG.quiet_hours_start}–{CFG.quiet_hours_end} "
                f"({CFG.quiet_hours_timezone})",
                f"**Status:** {quiet_status_line()}",
                "",
                "During quiet hours Clawy stays silent on chat. "
                "Moderation continues normally.",
            ]
            await reply_permanent(ctx, "\n".join(lines))
            return

        s = sub.strip().lower()
        if s in ("on", "enable", "true"):
            CFG.state.quiet_hours_enabled_override = True
            await ack(ctx, f"Quiet hours enabled. Window: "
                           f"{CFG.quiet_hours_start}–{CFG.quiet_hours_end} "
                           f"({CFG.quiet_hours_timezone}).")
            return
        if s in ("off", "disable", "false"):
            CFG.state.quiet_hours_enabled_override = False
            await ack(ctx, "Quiet hours disabled.")
            return
        if s == "reset":
            CFG.state.quiet_hours_enabled_override = None
            CFG.state.quiet_hours_start_override = None
            CFG.state.quiet_hours_end_override = None
            CFG.state.quiet_hours_timezone_override = None
            await ack(ctx, "Session overrides cleared. Using YAML values again.")
            return
        if s == "set":
            if not start or not end:
                await ack(ctx, "Usage: `!quiet set HH:MM HH:MM [Timezone]`")
                return
            # Cheap validation — _parse_hhmm lives in gating
            from core.gating import _parse_hhmm
            if _parse_hhmm(start) is None or _parse_hhmm(end) is None:
                await ack(ctx, "Invalid time format. Use HH:MM (e.g. `23:00`).")
                return
            CFG.state.quiet_hours_start_override = start
            CFG.state.quiet_hours_end_override = end
            if tz:
                # Validate timezone before accepting
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(tz)
                    CFG.state.quiet_hours_timezone_override = tz
                except Exception:
                    await ack(ctx, f"Unknown timezone `{tz}`. Keeping previous value.")
            CFG.state.quiet_hours_enabled_override = True
            await ack(ctx, f"Quiet hours set: {start}–{end} "
                           f"({CFG.quiet_hours_timezone}). Enabled.")
            return
        await ack(ctx, "Use `!quiet on|off|set HH:MM HH:MM [TZ]|reset`.")

    @commands.command(name="chatroles")
    async def chatroles_cmd(
        self, ctx: commands.Context, sub: str = "", *, rolename: str = ""
    ) -> None:
        """Role allowlist — which roles may chat with Clawy.

        Empty list = everyone can chat (default).
        Non-empty = ONLY members of those roles get replies.
        Moderation applies to everyone regardless.

        Usage:
          !chatroles                    show current allowlist
          !chatroles add Regular        add a role
          !chatroles remove Regular     remove a role
          !chatroles clear              empty the list (everyone chats)
          !chatroles reset              drop session override, use YAML
        """
        def _current() -> list[str]:
            return list(CFG.chat_allowed_roles)

        def _apply(new_list: list[str]) -> None:
            CFG.state.chat_allowed_roles_override = list(new_list)

        s = sub.strip().lower()
        if not s:
            roles = _current()
            if roles:
                body = "Only these roles can chat with me:\n" + "\n".join(f"  • {r}" for r in roles)
            else:
                body = "Everyone can chat with me (no role allowlist set)."
            await reply_permanent(ctx, f"**Chat allowlist:**\n{body}")
            return
        if s == "add":
            if not rolename:
                await ack(ctx, "Usage: `!chatroles add <role>`")
                return
            roles = _current()
            if rolename in roles:
                await ack(ctx, f"`{rolename}` is already on the list.")
                return
            roles.append(rolename)
            _apply(roles)
            await ack(ctx, f"Added `{rolename}` to chat allowlist. "
                           f"Now {len(roles)} role(s).")
            return
        if s == "remove":
            if not rolename:
                await ack(ctx, "Usage: `!chatroles remove <role>`")
                return
            roles = _current()
            if rolename not in roles:
                await ack(ctx, f"`{rolename}` is not on the list.")
                return
            roles.remove(rolename)
            _apply(roles)
            await ack(ctx, f"Removed `{rolename}`. "
                           + ("Allowlist now empty (everyone chats)." if not roles
                              else f"Now {len(roles)} role(s)."))
            return
        if s == "clear":
            _apply([])
            await ack(ctx, "Chat allowlist cleared. Everyone can chat with me.")
            return
        if s == "reset":
            CFG.state.chat_allowed_roles_override = None
            await ack(ctx, "Session override cleared. Using YAML values again.")
            return
        await ack(ctx, "Use `!chatroles [add|remove <role>|clear|reset]`.")

    @commands.command(name="proactive")
    async def proactive_cmd(self, ctx: commands.Context, arg: str = "") -> None:
        """Chance that Clawy replies unsolicited. Usage:
          !proactive            show current chance
          !proactive 0.03       set to 3%
          !proactive off        set to 0 (disabled)
          !proactive reset      drop override, use YAML
        """
        if not arg:
            yaml_val = float(CFG.raw.get("moderation", {}).get("proactive_reply_chance", 0.0))
            override = CFG.state.proactive_chance_override
            src = "override" if override is not None else "config"
            await reply_permanent(
                ctx,
                f"**Proactive chance:** {CFG.proactive_reply_chance:.3f} "
                f"({CFG.proactive_reply_chance*100:.1f}%) (source: {src})\n"
                f"YAML default: {yaml_val:.3f}\n"
                f"Cooldown: {CFG.mod.get('proactive_reply_cooldown_seconds', 300)}s"
            )
            return
        a = arg.strip().lower()
        if a in ("off", "disable", "0", "none"):
            CFG.state.proactive_chance_override = 0.0
            await ack(ctx, "Proactive replies disabled.")
            return
        if a == "reset":
            CFG.state.proactive_chance_override = None
            await ack(ctx, "Override cleared. Using YAML value.")
            return
        try:
            val = float(a)
        except ValueError:
            await ack(ctx, "Usage: `!proactive <float 0-1> | off | reset`")
            return
        if val < 0.0 or val > 1.0:
            await ack(ctx, "Value must be between 0.0 and 1.0.")
            return
        CFG.state.proactive_chance_override = val
        await ack(ctx, f"Proactive chance set to {val:.3f} ({val*100:.1f}%).")

    # ---------- NSFW channel management ----------
    @commands.command(name="nsfw")
    async def nsfw_cmd(self, ctx: commands.Context, sub: str = "", *, channel_name: str = "") -> None:
        """Manage NSFW/adult channel list (session-only).

        Usage:
          !nsfw                   show NSFW channels
          !nsfw add <name>        add a channel name
          !nsfw remove <name>     remove a channel name
        """
        if not sub:
            channels = CFG.nsfw_channels
            if channels:
                await reply_permanent(ctx,
                    f"**NSFW channels:** {', '.join(channels)}\n"
                    f"Adult content is tolerated in these channels."
                )
            else:
                await reply_permanent(ctx, "No NSFW channels configured. Use `!nsfw add <name>` to add one.")
            return
        s = sub.strip().lower()
        name = channel_name.strip().lstrip("#") if channel_name else ""
        if s == "add":
            if not name:
                await ack(ctx, "Usage: `!nsfw add <channel-name>`")
                return
            current = list(CFG.raw.get("nsfw_channels", []))
            if name in current:
                await ack(ctx, f"`{name}` is already in the NSFW list.")
                return
            current.append(name)
            CFG.raw["nsfw_channels"] = current
            await ack(ctx, f"Added `{name}` to NSFW channels (session-only).")
            return
        if s == "remove":
            if not name:
                await ack(ctx, "Usage: `!nsfw remove <channel-name>`")
                return
            current = list(CFG.raw.get("nsfw_channels", []))
            if name not in current:
                await ack(ctx, f"`{name}` is not in the NSFW list.")
                return
            current.remove(name)
            CFG.raw["nsfw_channels"] = current
            await ack(ctx, f"Removed `{name}` from NSFW channels.")
            return
        await ack(ctx, "Usage: `!nsfw [add|remove <name>]`")

    # ---------- dynamic mood toggle ----------
    @commands.command(name="dynmood")
    async def dynmood_cmd(self, ctx: commands.Context, arg: str = "") -> None:
        """Toggle dynamic mood switching. When on, the LLM can change
        its own mood based on conversation context.

        Usage:
          !dynmood           show current state
          !dynmood on        enable
          !dynmood off       disable
        """
        if not arg:
            state = "on" if CFG.dynamic_mood else "off"
            await reply_permanent(ctx,
                f"**Dynamic mood:** {state}\n"
                f"When on, the LLM can switch moods autonomously based on context.\n"
                f"Current mood: **{PERSONAS.active_mood}**"
            )
            return
        a = arg.strip().lower()
        if a in ("on", "true", "1", "yes", "enable"):
            CFG.raw["dynamic_mood"] = True
            await ack(ctx, "Dynamic mood enabled. The LLM can now switch moods on its own.")
        elif a in ("off", "false", "0", "no", "disable"):
            CFG.raw["dynamic_mood"] = False
            await ack(ctx, "Dynamic mood disabled. Only `!mood` changes moods now.")
        else:
            await ack(ctx, "Usage: `!dynmood on` / `!dynmood off`")

    @commands.command(name="perms")
    async def perms(self, ctx: commands.Context) -> None:
        """Show which permissions Clawy has in THIS channel."""
        if ctx.guild is None:
            await ack(ctx, "This command only works in a server channel, not DMs.")
            return
        me = ctx.guild.me
        if me is None:
            try:
                me = await ctx.guild.fetch_member(self.bot.user.id)
            except Exception as e:
                await ack(ctx, f"Could not read my own member object: `{e}`")
                return

        try:
            p = ctx.channel.permissions_for(me)
        except Exception as e:
            await ack(ctx, f"permissions_for failed: `{e}`")
            return

        needed = {
            "View Channel":         p.view_channel,
            "Send Messages":        p.send_messages,
            "Read History":         p.read_message_history,
            "Manage Messages":      p.manage_messages,
            "Manage Webhooks":      p.manage_webhooks,
            "Moderate Members":     p.moderate_members,
            "Kick Members":         p.kick_members,
            "Ban Members":          p.ban_members,
            "Manage Roles":         p.manage_roles,
        }
        top_role = me.top_role.name if me.top_role else "(none)"
        top_pos = me.top_role.position if me.top_role else -1

        lines = [f"**Clawy's permissions in {ctx.channel.mention}:**"]
        for name, ok in needed.items():
            lines.append(f"  {'✅' if ok else '❌'} {name}")
        lines.append("")
        lines.append(f"**Top role:** `{top_role}` (position {top_pos})")
        lines.append("Can only moderate members whose highest role is *below* this.")
        await reply_permanent(ctx, "\n".join(lines))

    # ---------- jump into conversation ----------
    @commands.command(name="jumpin")
    async def jumpin(self, ctx: commands.Context, count: int = 5) -> None:
        """Make Clawy jump uninvited into the last N messages of this channel.

        Usage:
          !jumpin        — react to the last 5 messages
          !jumpin 10     — react to the last 10 messages (max 20)
        """
        count = max(1, min(count, 20))  # clamp 1-20

        # NOTE: cog_before_invoke already deleted the !jumpin command message

        if not await OLLAMA.health():
            await ack(ctx, "❌ Ollama is not reachable.")
            return

        # Fetch recent messages
        msgs: list[discord.Message] = []
        try:
            async for m in ctx.channel.history(limit=count + 5):
                if m.author.bot:
                    continue
                if m.content.startswith(CFG.command_prefix):
                    continue
                if not m.content.strip():
                    continue
                msgs.append(m)
                if len(msgs) >= count:
                    break
        except discord.DiscordException as e:
            log.warning("!jumpin: failed to read history: %s", e)
            await ack(ctx, "❌ Cannot read channel history.")
            return

        if not msgs:
            await ack(ctx, "No recent messages to react to.")
            return

        # Build a conversation snapshot — oldest first
        msgs.reverse()
        convo = "\n".join(
            f"{m.author.display_name}: {m.content[:300]}"
            for m in msgs
        )

        from core.prompts import build_chat_system_prompt
        system = build_chat_system_prompt(is_owner=False, channel_name=ctx.channel.name)

        user_prompt = (
            f"You are watching this conversation in #{ctx.channel.name} and decide to jump in "
            f"unprompted, in character — witty, on-topic, and true to your persona. "
            f"Do not address any one person specifically unless it feels natural.\n\n"
            f"Recent messages:\n{convo}"
        )

        try:
            async with ctx.channel.typing():
                result = await asyncio.wait_for(
                    OLLAMA.generate_json(system, user_prompt),
                    timeout=CFG.ollama_timeout + 10,
                )
        except asyncio.TimeoutError:
            log.warning("!jumpin: Ollama timed out")
            await ack(ctx, "⏱️ Ollama timed out.")
            return
        except Exception as e:
            log.warning("!jumpin: Ollama error: %s", e)
            await ack(ctx, f"❌ Ollama error: {type(e).__name__}")
            return

        if not isinstance(result, dict):
            log.warning("!jumpin: non-dict result: %r", result)
            await ack(ctx, "❌ Model returned invalid JSON.")
            return

        text = str(result.get("message", "")).strip()[:1800]
        if not text:
            log.warning("!jumpin: empty message in result: %r", result)
            await ack(ctx, "❌ Model returned an empty reply.")
            return

        try:
            await send_with_extras(ctx.channel, text, result, cfg=CFG)
        except discord.DiscordException as e:
            log.warning("!jumpin send failed: %s", e)
            await ack(ctx, f"❌ Send failed: {type(e).__name__}")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
