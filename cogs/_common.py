"""
Shared helpers for command-based cogs.

Problem: admin !commands (e.g. `!moveto #nsfw`) linger in channels. Even after
a command succeeds, the original message stays unless each command remembers
to delete it. Easy to forget, and non-admin attempts don't get deleted at all.

Solution: cogs inherit from `CleanCommandCog`. It:
  1. Deletes `ctx.message` BEFORE the command body runs (via cog_before_invoke).
  2. Deletes `ctx.message` when a non-admin fails cog_check.
  3. Provides `ack()` for short transient replies that self-delete in the
     source channel, and `reply_permanent()` for informational output
     that routes to the configured log channel (so regular users don't
     see admin diagnostics like !perms, !whois, !strikes).

Commands should use:
  - `await ack(ctx, "...")`              for "done" / "ok" style confirmations
                                          (source channel, 6s, self-deletes)
  - `await reply_permanent(ctx, "...")`  for admin output that should be kept
                                          (log channel if set, else source)

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
    """Delete the invoking !command message.

    Failures are logged but don't raise. The most common silent failure is
    missing 'Manage Messages' permission in the source channel — if you see
    your !command staying visible despite a successful reply, check:
        !perms
    in that channel and look for ❌ Manage Messages.
    """
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        log.warning(
            "Cannot delete command in #%s — missing Manage Messages",
            getattr(ctx.channel, "name", "?"),
        )
    except discord.NotFound:
        pass  # Already gone, fine.
    except discord.HTTPException as e:
        log.warning("Delete failed: %s", e)


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
    """Informational reply meant for admin eyes only.

    Routing:
      - If `CFG.log_channel_id` is set AND it points to a channel the bot
        can write to AND it is DIFFERENT from the source channel, post there.
        A tiny breadcrumb ack stays in the source channel ("Sent to #log.").
      - Otherwise (no log channel, unresolvable, or admin ran the command
        *in* the log channel), post inline in the source channel.

    Use for !diag, !whois, !strikes, !recall, !persona (listing), !perms,
    !sleepstatus — anything regular users shouldn't see. Posts as Clawy,
    no reply chain (the original command is already deleted).
    """
    # Late import to avoid circular — cogs import _common which would pull CFG.
    from core.config import CFG

    text = text[:1900]
    target: discord.abc.Messageable | None = None
    is_remote = False

    if CFG.log_channel_id and ctx.guild is not None:
        log_ch = ctx.guild.get_channel(CFG.log_channel_id)
        if (
            isinstance(log_ch, discord.TextChannel)
            and log_ch.id != ctx.channel.id
            and log_ch.permissions_for(ctx.guild.me).send_messages
        ):
            target = log_ch
            is_remote = True

    if target is None:
        target = ctx.channel

    try:
        sent = await target.send(
            text,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.DiscordException as e:
        log.warning("permanent reply send failed: %s", e)
        return None

    # Breadcrumb in the source channel so the admin knows where the reply went.
    if is_remote:
        try:
            await ctx.channel.send(
                f"{ctx.author.mention} Sent to {target.mention}.",
                allowed_mentions=discord.AllowedMentions(users=True),
                delete_after=ACK_LINGER_SECONDS,
            )
        except discord.DiscordException:
            pass

    return sent


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
