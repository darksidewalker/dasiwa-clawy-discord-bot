"""Owner/admin-only runtime controls."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import discord
from discord.ext import commands

from core.config import CFG, VALID_MODES
from core.executor import execute_ban, execute_kick, execute_mute
from core.gating import in_quiet_hours, quiet_status_line
from core.ollama_client import OLLAMA
from core.persona import PERSONAS
from core.store import STORE

from ._common import CleanCommandCog, ack, reply_permanent

log = logging.getLogger(__name__)


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
                ("sleep",   "silence Clawy (optionally for a duration)"),
                ("wake",    "wake Clawy from sleep"),
                ("sleepstatus", "show sleep state"),
            ]),
            ("Mode & persona", [
                ("mode",    "show/switch bot mode"),
                ("persona", "show/switch persona (or reload)"),
                ("mood",    "show/switch mood for active persona"),
                ("model",   "show/switch Ollama model (session)"),
                ("think",   "toggle Ollama reasoning trace on/off"),
            ]),
            ("Chat gating", [
                ("quiet",      "scheduled quiet hours — Clawy silent"),
                ("chatroles",  "role allowlist — who Clawy chats with"),
                ("proactive",  "chance of unsolicited replies"),
                ("jumpin",     "make Clawy jump into the last N channel messages"),
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
            ("Roles engine", [
                ("roles",    "manage activity-based role rules"),
            ]),
            ("Diagnostics", [
                ("diag",     "health check: Ollama, mode, gating"),
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
    async def diag(self, ctx: commands.Context) -> None:
        t0 = time.time()
        healthy = await OLLAMA.health()
        latency_ms = int((time.time() - t0) * 1000)
        lines = [
            f"**Ollama**: {'✅ reachable' if healthy else '❌ unreachable'} at `{CFG.ollama_url}` ({latency_ms}ms)",
            f"**Model**: `{CFG.model}`  |  **Think**: {'on' if CFG.think else 'off'}",
            f"**Mode**: `{CFG.mode}`  |  **Paused**: {CFG.state.paused}",
            f"**Owner ID**: `{CFG.owner_id}`  |  **Your ID**: `{ctx.author.id}`"
            + (" ✅ match" if ctx.author.id == CFG.owner_id else " ❌ NO MATCH"),
            f"**Persona**: `{PERSONAS.active_key}` / mood `{PERSONAS.active_mood}`",
            f"**DB**: `{CFG.db_path}`",
            f"**Allowed actions**: {', '.join(sorted(CFG.allowed_actions))}",
            f"**Quiet hours**: "
            f"{'on' if CFG.quiet_hours_enabled else 'off'}"
            + (f" ({'ACTIVE' if in_quiet_hours() else 'idle'})" if CFG.quiet_hours_enabled else "")
            + f" | window {CFG.quiet_hours_start}–{CFG.quiet_hours_end} {CFG.quiet_hours_timezone}",
            f"**Chat allowlist**: "
            + (", ".join(CFG.chat_allowed_roles) if CFG.chat_allowed_roles else "everyone"),
            f"**Proactive chance**: {CFG.proactive_reply_chance:.3f}",
        ]
        await reply_permanent(ctx, "\n".join(lines))

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
            await ack(ctx, f"No record of **{member.display_name}** yet.")
            return
        first = time.strftime("%Y-%m-%d", time.localtime(info["first_seen"]))
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(info["last_seen"]))
        await reply_permanent(ctx,
            f"**{info['display_name']}**\n"
            f"• First seen: {first}\n"
            f"• Last seen: {last}\n"
            f"• Messages observed: {info['msg_count']}\n"
            f"• Notes: {info['notes'] or '(none)'}"
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
        turns = await STORE.recent_chat_turns(member.id, limit=10)
        if not turns:
            await ack(ctx, f"No chat memory for **{member.display_name}**.")
            return
        lines = [f"**Chat memory with {member.display_name}** (most recent {len(turns)}):"]
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
        system = build_chat_system_prompt(is_owner=False)

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
            await ctx.channel.send(text)
        except discord.DiscordException as e:
            log.warning("!jumpin send failed: %s", e)
            await ack(ctx, f"❌ Send failed: {type(e).__name__}")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
