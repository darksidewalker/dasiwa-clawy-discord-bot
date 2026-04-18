"""
Shared helpers for command-based cogs.

Problem: admin !commands (e.g. `!moveto #nsfw`) linger in channels. Even after
a command succeeds, the original message stays unless each command remembers
to delete it. Easy to forget, and non-admin attempts don't get deleted at all.

Solution: cogs inherit from `CleanCommandCog`. It:
  1. Deletes `ctx.message` BEFORE the command body runs (via cog_before_invoke).
  2. Deletes `ctx.message` when a non-admin fails cog_check.
  3. Provides `ack()` for short transient replies that self-delete,
     and `reply_permanent()` for informational output that should stick.

Commands should use:
  - `await ack(ctx, "...")`              for "done" / "ok" style confirmations
  - `await reply_permanent(ctx, "...")`  for output the admin needs to read

Both post as the bot's own Discord identity (Clawy), since that's the voice.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# How long transient acks stay visible before auto-delete (seconds)
ACK_LINGER_SECONDS = 6


async def delete_cmd(ctx: commands.Context) -> None:
    """Delete the invoking !command message. Silent on failure."""
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def ack(
    ctx: commands.Context,
    text: str,
    *,
    linger: int = ACK_LINGER_SECONDS,
) -> discord.Message | None:
    """Short confirmation that auto-deletes after `linger` seconds.

    Use for transient feedback like 'Paused', 'Mode set to X', 'Already sleeping',
    usage hints, and errors. Posts as the bot (Clawy) via plain channel.send —
    no reply chain (the original command is gone anyway).
    """
    try:
        return await ctx.channel.send(
            text,
            allowed_mentions=discord.AllowedMentions.none(),
            delete_after=linger,
        )
    except discord.DiscordException as e:
        log.debug("ack send failed: %s", e)
        return None


async def reply_permanent(
    ctx: commands.Context,
    text: str,
) -> discord.Message | None:
    """Informational reply that sticks.

    Use for !diag, !whois, !strikes, !recall, !persona (listing), !mood (listing),
    !sleepstatus — anywhere the admin actually needs to read the output.
    Posts as the bot (Clawy), no reply chain (original command is gone).
    """
    try:
        return await ctx.channel.send(
            text[:1900],
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.DiscordException as e:
        log.warning("permanent reply send failed: %s", e)
        return None


class CleanCommandCog(commands.Cog):
    """Base cog that automatically cleans up !command messages.

    Inherit from this instead of `commands.Cog`. Every command registered on
    the subclass will have its invoking message deleted before the body runs.

    Override `is_authorized(ctx)` to gate access; failed auth still deletes
    the command. Default: allow everyone (subclasses typically tighten this).
    """

    def is_authorized(self, ctx: commands.Context) -> bool:
        """Override in subclasses to gate command access."""
        return True

    async def cog_check(self, ctx: commands.Context) -> bool:  # type: ignore[override]
        if self.is_authorized(ctx):
            return True
        # Non-authorized users: silently delete their attempt.
        await delete_cmd(ctx)
        return False

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        """Runs after cog_check passes, before the command body.

        This is the happy-path delete: authorized user, command parsed OK,
        we wipe the !command message so only the bot's response remains.
        """
        await delete_cmd(ctx)
