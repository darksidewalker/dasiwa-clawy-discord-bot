"""
Sleep mode for Clawy.

!sleep          — sleep indefinitely (until !wake)
!sleep 30m      — sleep for 30 minutes, then auto-wake
!sleep 2h       — sleep for 2 hours
!sleep 1h30m    — sleep for 1 hour 30 minutes
!wake           — wake immediately

While sleeping:
  - Discord status → Do Not Disturb with a custom status
  - All messages are silently ignored (mod, chat, mention rate-limit)
  - Move commands still work for admins (move cog doesn't check sleep)
  - Admin commands still work
  - Auto-wake restores the active status
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import discord
from discord.ext import commands, tasks

from core.config import CFG
from core.store import STORE

from ._common import CleanCommandCog, ack, reply_permanent

log = logging.getLogger(__name__)

# Status text shown while sleeping
_SLEEP_STATUS = "Resting... do not disturb."
_AWAKE_STATUS = "Watching the realm."


def _is_admin(ctx: commands.Context) -> bool:
    if ctx.author.id == CFG.owner_id:
        return True
    if isinstance(ctx.author, discord.Member):
        return ctx.author.guild_permissions.administrator
    return False


def _parse_duration(text: str) -> int | None:
    """
    Parse a human duration string into seconds.
    Accepts: 30m  2h  1h30m  90s  1h30m20s
    Returns None if unparseable.
    """
    text = text.strip().lower()
    pattern = r'^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$'
    m = re.match(pattern, text)
    if not m or not any(m.groups()):
        return None
    hours   = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def _format_duration(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s:
        parts.append(f"{s}s")
    return "".join(parts) or "0s"


class SleepCog(CleanCommandCog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wake_task: asyncio.Task | None = None
        self._check_autowake.start()

    def cog_unload(self) -> None:
        self._check_autowake.cancel()
        if self._wake_task and not self._wake_task.done():
            self._wake_task.cancel()

    def is_authorized(self, ctx: commands.Context) -> bool:
        return _is_admin(ctx)

    # ── Presence helpers ────────────────────────────────────────────────

    async def _set_sleeping_presence(self) -> None:
        try:
            await self.bot.change_presence(
                status=discord.Status.do_not_disturb,
                activity=discord.CustomActivity(name=_SLEEP_STATUS),
            )
        except Exception as e:
            log.warning("Failed to set sleep presence: %s", e)

    async def _set_awake_presence(self) -> None:
        try:
            await self.bot.change_presence(
                status=discord.Status.online,
                activity=discord.CustomActivity(name=_AWAKE_STATUS),
            )
        except Exception as e:
            log.warning("Failed to set awake presence: %s", e)

    # ── Core sleep / wake logic ─────────────────────────────────────────

    async def _do_sleep(self, ctx: commands.Context, duration_s: int | None) -> None:
        CFG.state.sleeping = True
        CFG.state.wake_at = (time.time() + duration_s) if duration_s else 0.0

        await self._set_sleeping_presence()

        if duration_s:
            label = _format_duration(duration_s)
            reply = f"Resting for **{label}**. Do not disturb."
        else:
            reply = "Going to sleep. Wake me with `!wake`."

        await reply_permanent(ctx, reply)

        await STORE.log_bot_action(
            kind="sleep",
            actor_id=ctx.author.id,
            summary=f"sleep for {_format_duration(duration_s) if duration_s else 'indefinite'}",
        )

        log.info("Sleep mode ON. Auto-wake at %s",
                 time.strftime('%H:%M:%S', time.localtime(CFG.state.wake_at))
                 if CFG.state.wake_at else "never")

    async def _do_wake(self, ctx: commands.Context | None = None) -> None:
        CFG.state.sleeping = False
        CFG.state.wake_at = 0.0

        await self._set_awake_presence()

        if ctx is not None:
            await reply_permanent(ctx, "I am awake.")

        await STORE.log_bot_action(
            kind="wake",
            actor_id=ctx.author.id if ctx else None,
            summary="woke up",
        )
        log.info("Sleep mode OFF.")

    # ── Commands ────────────────────────────────────────────────────────

    @commands.command(name="sleep")
    async def sleep_cmd(self, ctx: commands.Context, duration: str = "") -> None:
        """
        Put Clawy to sleep.
        Usage:
          !sleep           — sleep until !wake
          !sleep 30m       — sleep for 30 minutes
          !sleep 2h        — sleep for 2 hours
          !sleep 1h30m     — sleep for 1 hour 30 minutes
        """
        if CFG.state.sleeping:
            remaining = CFG.state.wake_at - time.time() if CFG.state.wake_at else 0
            if remaining > 0:
                await ack(ctx,
                    f"Already sleeping. Auto-wake in **{_format_duration(int(remaining))}**. "
                    f"Use `!wake` to wake now."
                )
            else:
                await ack(ctx, "Already sleeping. Use `!wake` to wake.")
            return

        duration_s: int | None = None
        if duration:
            duration_s = _parse_duration(duration)
            if duration_s is None:
                await ack(ctx,
                    "Could not parse that duration. "
                    "Examples: `!sleep 30m` `!sleep 2h` `!sleep 1h30m`"
                )
                return

        await self._do_sleep(ctx, duration_s)

    @commands.command(name="wake")
    async def wake_cmd(self, ctx: commands.Context) -> None:
        """Wake Clawy up from sleep mode."""
        if not CFG.state.sleeping:
            await ack(ctx, "I am already awake.")
            return
        await self._do_wake(ctx)

    @commands.command(name="sleepstatus")
    async def sleepstatus_cmd(self, ctx: commands.Context) -> None:
        """Show whether Clawy is sleeping and when she'll wake."""
        if not CFG.state.sleeping:
            await reply_permanent(ctx, "Awake and watching.")
            return
        if CFG.state.wake_at:
            remaining = max(0, CFG.state.wake_at - time.time())
            await reply_permanent(ctx,
                f"Sleeping. Auto-wake in **{_format_duration(int(remaining))}**."
            )
        else:
            await reply_permanent(ctx, "Sleeping indefinitely. Use `!wake` to wake.")

    # ── Auto-wake background task ────────────────────────────────────────

    @tasks.loop(seconds=15)
    async def _check_autowake(self) -> None:
        """Runs every 15 seconds. Wakes Clawy when wake_at is reached."""
        if not CFG.state.sleeping:
            return
        if not CFG.state.wake_at:
            return
        if time.time() >= CFG.state.wake_at:
            await self._do_wake(ctx=None)
            # Announce the auto-wake in the log channel if configured
            if CFG.log_channel_id:
                guild = next((g for g in self.bot.guilds
                              if CFG.guild_id == 0 or g.id == CFG.guild_id), None)
                if guild:
                    ch = guild.get_channel(CFG.log_channel_id)
                    if isinstance(ch, discord.TextChannel):
                        try:
                            await ch.send("🌅 Clawy has auto-woken from sleep.")
                        except discord.DiscordException:
                            pass

    @_check_autowake.before_loop
    async def _before_check(self) -> None:
        await self.bot.wait_until_ready()

    # ── Set presence on bot ready ───────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if CFG.state.sleeping:
            await self._set_sleeping_presence()
        else:
            await self._set_awake_presence()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SleepCog(bot))
