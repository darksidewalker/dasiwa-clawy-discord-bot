"""
Move messages between channels.

Discord does not let bots literally move messages, so we:
  1. Ensure we have (or create) a webhook in the destination channel.
  2. Re-download each source message's attachments.
  3. Re-post via the webhook using the original author's display name + avatar
     (so it looks like the author posted it in the destination).
  4. Delete the original messages in the source channel.
  5. Log the move to bot_actions.

Two command forms:
  !moveto #channel [N]
      Reply to a message. Moves THAT message and optionally the next
      up-to-N messages in the same channel from the same author.

  !movelast @user N #channel
      Moves the last N messages from @user in the current channel to #channel.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from typing import Sequence

import aiohttp
import discord
from discord.ext import commands

from core.config import CFG
from core.store import STORE

log = logging.getLogger(__name__)

WEBHOOK_NAME = "persona-mover"   # bot-managed webhook name we look for / create


def _is_admin(ctx: commands.Context) -> bool:
    if ctx.author.id == CFG.owner_id:
        return True
    if isinstance(ctx.author, discord.Member):
        perms = ctx.author.guild_permissions
        return perms.administrator or perms.manage_messages
    return False


async def _get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook | None:
    try:
        hooks = await channel.webhooks()
    except discord.Forbidden:
        return None
    for h in hooks:
        if h.name == WEBHOOK_NAME and h.token:
            return h
    try:
        return await channel.create_webhook(name=WEBHOOK_NAME, reason="message-mover")
    except discord.Forbidden:
        return None


async def _download_attachments(
    session: aiohttp.ClientSession, atts: Sequence[discord.Attachment]
) -> list[discord.File]:
    files: list[discord.File] = []
    for a in atts:
        # Discord caps uploads; skip oversized ones to avoid a noisy failure.
        # We don't know the destination's boost tier, so be conservative (25 MB).
        if a.size > 25 * 1024 * 1024:
            log.info("skipping oversized attachment %s (%.1f MB)", a.filename, a.size / 1024 / 1024)
            continue
        try:
            async with session.get(a.url) as r:
                if r.status != 200:
                    log.warning("attachment fetch HTTP %s for %s", r.status, a.filename)
                    continue
                data = await r.read()
        except Exception as e:
            log.warning("attachment fetch failed for %s: %s", a.filename, e)
            continue
        files.append(discord.File(io.BytesIO(data), filename=a.filename, spoiler=a.is_spoiler()))
    return files


async def _repost_via_webhook(
    webhook: discord.Webhook,
    author: discord.abc.User,
    content: str,
    files: list[discord.File],
) -> bool:
    avatar_url = author.display_avatar.url if author.display_avatar else None
    username = (author.display_name or "user")[:80]
    try:
        await webhook.send(
            content=content or "\u200b",   # zero-width space if only attachments
            username=username,
            avatar_url=avatar_url,
            files=files,
            allowed_mentions=discord.AllowedMentions.none(),  # don't re-ping people
        )
        return True
    except discord.DiscordException as e:
        log.warning("webhook send failed: %s", e)
        return False


async def _post_to_log(guild: discord.Guild | None, text: str) -> None:
    """Post a message to the configured log channel, if any."""
    if guild is None or not CFG.log_channel_id:
        return
    ch = guild.get_channel(CFG.log_channel_id)
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException as e:
            log.warning("log channel send failed: %s", e)


class MoveCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._http: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        self._http = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    async def cog_check(self, ctx: commands.Context) -> bool:
        return _is_admin(ctx)

    # =====================================================
    # !moveto #channel [N]
    # =====================================================
    @commands.command(name="moveto")
    async def moveto(
        self,
        ctx: commands.Context,
        dest: discord.TextChannel | None = None,
        follow_count: int = 0,
    ) -> None:
        """Reply to a message with '!moveto #channel [N]'.
        Moves that message + up to N subsequent messages from the same author
        in the same channel.
        """
        if dest is None:
            await ctx.reply("Usage: reply to a message, then: `!moveto #channel [N]`")
            return
        if ctx.message.reference is None or ctx.message.reference.message_id is None:
            await ctx.reply("You need to **reply** to the message you want to move.")
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.reply("This command only works in text channels.")
            return
        if dest.id == ctx.channel.id:
            await ctx.reply("Destination is the same as source.")
            return

        try:
            anchor = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except discord.NotFound:
            await ctx.reply("Could not find the referenced message.")
            return

        follow_count = max(0, min(follow_count, CFG.move_max_batch - 1))

        # Collect: anchor + next N messages in source channel from SAME author
        batch: list[discord.Message] = [anchor]
        if follow_count > 0:
            async for m in ctx.channel.history(after=anchor, oldest_first=True, limit=200):
                if len(batch) >= follow_count + 1:
                    break
                if m.author.id == anchor.author.id:
                    batch.append(m)

        await self._perform_move(ctx, batch, dest, anchor.author)

    # =====================================================
    # !movelast @user N [#channel]
    # =====================================================
    @commands.command(name="movelast")
    async def movelast(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        n: int = 1,
        dest: discord.TextChannel | None = None,
    ) -> None:
        """Move the last N messages from @user in this channel to #channel."""
        if member is None or dest is None:
            await ctx.reply("Usage: `!movelast @user N #channel`")
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.reply("This command only works in text channels.")
            return
        if dest.id == ctx.channel.id:
            await ctx.reply("Destination is the same as source.")
            return
        n = max(1, min(n, CFG.move_max_batch))

        # Walk backwards from current point, collect up to n msgs from member
        found: list[discord.Message] = []
        async for m in ctx.channel.history(limit=500):
            if m.id == ctx.message.id:
                continue   # don't move the command itself
            if m.author.id == member.id:
                found.append(m)
                if len(found) >= n:
                    break
        if not found:
            await ctx.reply(f"No recent messages from **{member.display_name}** found.")
            return
        # Chronological order for reposting
        found.reverse()
        await self._perform_move(ctx, found, dest, member)

    # =====================================================
    # core move logic
    # =====================================================
    async def _perform_move(
        self,
        ctx: commands.Context,
        messages: list[discord.Message],
        dest: discord.TextChannel,
        author: discord.abc.User,
    ) -> None:
        if not messages:
            await ctx.reply("Nothing to move.")
            return

        # Permission sanity checks
        me = ctx.guild.me if ctx.guild else None
        if me is None:
            await ctx.reply("No guild context.")
            return
        src_perms = ctx.channel.permissions_for(me)
        dst_perms = dest.permissions_for(me)
        if not dst_perms.manage_webhooks:
            await ctx.reply(f"I need **Manage Webhooks** in {dest.mention}.")
            return
        if not src_perms.manage_messages:
            await ctx.reply("I need **Manage Messages** here to delete the originals.")
            return

        webhook = await _get_or_create_webhook(dest)
        if webhook is None:
            await ctx.reply(f"Could not create/find a webhook in {dest.mention}.")
            return

        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()

        # Delete the command message so the operation is invisible to regular users
        try:
            await ctx.message.delete()
        except discord.DiscordException:
            pass

        moved = 0
        failed = 0
        for src in messages:
            files = await _download_attachments(self._http, src.attachments)
            ok = await _repost_via_webhook(webhook, author, src.content or "", files)
            if not ok:
                failed += 1
                continue
            try:
                await src.delete()
                moved += 1
            except discord.Forbidden:
                failed += 1
                log.warning("could not delete %s after reposting", src.id)
            except discord.NotFound:
                moved += 1   # already gone, count it as success
            await asyncio.sleep(0.3)

        if moved == 0:
            return  # nothing happened, stay silent

        # ── Notify the affected user in the SOURCE channel (auto-deletes after 8s) ──
        noun = "message" if moved == 1 else "messages"
        try:
            notice = await ctx.channel.send(
                f"{author.mention} Your {noun} {'was' if moved == 1 else 'were'} "
                f"moved to {dest.mention}.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            # Auto-delete after 8 seconds so it doesn't clutter the channel
            await asyncio.sleep(8)
            await notice.delete()
        except discord.DiscordException:
            pass

        # ── Log to admin log channel (private, full detail) ──
        await _post_to_log(
            guild=ctx.guild,
            text=(
                f"📦 **Move** | {moved} {noun} from {ctx.channel.mention} → {dest.mention}\n"
                f"👤 User: {author.mention} (`{author.id}`)\n"
                f"🛡️ By: {ctx.author.mention}\n"
                + (f"⚠️ {failed} failed to move." if failed else "")
            ),
        )

        # ── Persist to bot_actions DB ──
        try:
            await STORE.log_bot_action(
                kind="move",
                actor_id=ctx.author.id,
                target_id=author.id,
                channel_id=ctx.channel.id,
                summary=f"{moved} msg(s) {ctx.channel.name} -> {dest.name}",
                extra=json.dumps({
                    "dest_channel_id": dest.id,
                    "moved": moved,
                    "failed": failed,
                    "ts": int(time.time()),
                }),
            )
        except Exception as e:
            log.warning("move log failed: %s", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MoveCog(bot))
