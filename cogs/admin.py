"""Owner/admin-only runtime controls."""
from __future__ import annotations

import json
import time

import discord
from discord.ext import commands

from core.config import CFG, VALID_MODES
from core.executor import execute_ban, execute_kick, execute_mute
from core.ollama_client import OLLAMA
from core.persona import PERSONAS
from core.store import STORE

from ._common import CleanCommandCog, ack, reply_permanent


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
            f"**Persona**: `{PERSONAS.active_key}` / mood `{PERSONAS.active_mood}`",
            f"**DB**: `{CFG.db_path}`",
            f"**Allowed actions**: {', '.join(sorted(CFG.allowed_actions))}",
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
            await ack(ctx, f"Removed timeout from **{member.display_name}**.",
                            allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await ack(ctx, "Missing permission to remove timeout.")

    @commands.command(name="perms")
    async def perms(self, ctx: commands.Context) -> None:
        """Show which permissions Clawy has in THIS channel."""
        me = ctx.guild.me
        p = ctx.channel.permissions_for(me)
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
        lines = [f"**Clawy's permissions in {ctx.channel.mention}:**"]
        for name, ok in needed.items():
            lines.append(f"  {'✅' if ok else '❌'} {name}")
        await reply_permanent(ctx, "\n".join(lines))

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
