"""
Cross-channel message deletion for admins.

Admin-only deterministic moderation: from any channel (typically a private
backstage/log channel) an admin can delete recent messages in another channel.

These commands are intentionally NOT exposed to the LLM. The LLM can never
delete more than the single message that triggered its judgment (via the
"delete" autonomous action in core/executor.py). Bulk deletion is always a
human decision.

Commands
--------
  !purge #channel N [@user]
      Delete the last N messages in #channel.
      Optional @user filter: only delete messages authored by that user.

  !purgeuser @user N [#channel]
      Delete the last N messages from @user.
      Optional #channel: defaults to the channel the command is run in
      (so an admin in #backstage typing `!purgeuser @bob 5 #general`
      cleans #general; running `!purgeuser @bob 5` in #general cleans
      the current channel).

Safety
------
  * Admin-only (owner_id or Administrator). Non-admins get the command
    silently deleted by CleanCommandCog.
  * Hard cap per call via CFG.move_max_batch (configurable, default 25).
  * Protected users (owner, server owner, configured protected_roles) are
    never touched, even if explicitly targeted — the command silently
    skips their messages and reports a count.
  * Discord's bulk-delete API only works on messages younger than 14 days;
    older messages are deleted one at a time (slower, rate-limited).
  * Every purge is logged to bot_actions and to the configured log channel
    with full attribution (who ran it, where, how many, against whom).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from core.config import CFG
from core.store import STORE

from ._common import CleanCommandCog, ack, reply_permanent

log = logging.getLogger(__name__)

# Discord's bulk-delete cutoff: messages older than 14 days must be deleted
# one at a time. We subtract a small safety margin to avoid edge-case 400s.
_BULK_DELETE_CUTOFF = timedelta(days=14, hours=-1)

# Polite delay between single deletes (older messages or fallback path) so we
# don't get rate-limited on long purges.
_SINGLE_DELETE_SLEEP = 0.35


def _is_admin(ctx: commands.Context) -> bool:
    if ctx.author.id == CFG.owner_id:
        return True
    if isinstance(ctx.author, discord.Member):
        perms = ctx.author.guild_permissions
        return perms.administrator or perms.manage_messages
    return False


def _is_protected(member: discord.abc.User, guild: discord.Guild) -> bool:
    """Mirror executor._is_protected, but tolerant of non-Member authors
    (webhook reposts, departed users) — those are NOT protected."""
    if member.id == CFG.owner_id:
        return True
    if guild.owner_id == member.id:
        return True
    if isinstance(member, discord.Member):
        return any(r.name in CFG.protected_roles for r in member.roles)
    return False


class PurgeCog(CleanCommandCog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def is_authorized(self, ctx: commands.Context) -> bool:
        return _is_admin(ctx)

    # =====================================================
    # !purge #channel N [@user]
    # =====================================================
    @commands.command(name="purge")
    async def purge(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        n: int = 0,
        member: discord.Member | None = None,
    ) -> None:
        """Delete the last N messages in #channel.

        Usage:
          !purge #channel N           — delete last N messages from anyone
          !purge #channel N @user     — delete last N messages by @user only

        N is capped at `move.max_batch` in config.yaml (default 25).
        """
        if channel is None or n <= 0:
            await ack(ctx, "Usage: `!purge #channel N [@user]`")
            return
        await self._do_purge(
            ctx,
            target_channel=channel,
            n=n,
            only_author=member,
        )

    # =====================================================
    # !purgeuser @user N [#channel]
    # =====================================================
    @commands.command(name="purgeuser")
    async def purgeuser(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        n: int = 0,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Delete the last N messages from @user.

        Usage:
          !purgeuser @user N             — last N from @user in THIS channel
          !purgeuser @user N #channel    — last N from @user in #channel

        N is capped at `move.max_batch` in config.yaml (default 25).
        """
        if member is None or n <= 0:
            await ack(ctx, "Usage: `!purgeuser @user N [#channel]`")
            return
        target = channel or (
            ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        )
        if target is None:
            await ack(ctx, "Specify a `#channel` — this command only works on text channels.")
            return
        await self._do_purge(
            ctx,
            target_channel=target,
            n=n,
            only_author=member,
        )

    # =====================================================
    # core purge logic
    # =====================================================
    async def _do_purge(
        self,
        ctx: commands.Context,
        *,
        target_channel: discord.TextChannel,
        n: int,
        only_author: discord.Member | None,
    ) -> None:
        if ctx.guild is None:
            await ack(ctx, "This command only works in a server.")
            return

        # Cap N to the configured maximum (shared with move.max_batch).
        cap = CFG.move_max_batch
        n = max(1, min(n, cap))

        # Permission sanity check on the TARGET channel (which may differ
        # from where the admin typed the command — that's the whole point).
        me = ctx.guild.me
        if me is None:
            await ack(ctx, "No guild member context for me.")
            return
        target_perms = target_channel.permissions_for(me)
        if not target_perms.read_message_history:
            await ack(ctx, f"I need **Read Message History** in {target_channel.mention}.")
            return
        if not target_perms.manage_messages:
            await ack(ctx, f"I need **Manage Messages** in {target_channel.mention}.")
            return

        # Walk history newest-first, collect up to N matching messages.
        # If only_author is set, we may need to read past more than N raw
        # messages — bound by a generous upper limit so we don't loop forever.
        scan_limit = max(n * 20, 200) if only_author else n
        scan_limit = min(scan_limit, 1000)

        to_delete: list[discord.Message] = []
        skipped_protected: list[str] = []
        try:
            async for m in target_channel.history(limit=scan_limit):
                # Don't try to delete the !purge command itself if we're
                # purging the same channel where it was typed.
                if m.id == ctx.message.id:
                    continue
                if only_author is not None and m.author.id != only_author.id:
                    continue
                # Refuse to delete protected users' messages, even on demand.
                # Same rule the LLM operates under — admins can still kick/ban
                # protected users themselves via Discord, but the BOT won't
                # delete their messages on someone else's instruction.
                if _is_protected(m.author, ctx.guild):
                    if m.author.display_name not in skipped_protected:
                        skipped_protected.append(m.author.display_name)
                    continue
                to_delete.append(m)
                if len(to_delete) >= n:
                    break
        except discord.Forbidden:
            await ack(ctx, f"Cannot read history of {target_channel.mention}.")
            return
        except discord.HTTPException as e:
            await ack(ctx, f"History read failed: `{e}`")
            return

        if not to_delete:
            who = f" from **{only_author.display_name}**" if only_author else ""
            await ack(ctx, f"No matching messages{who} found in {target_channel.mention}.")
            return

        # Split into bulk-deletable (< 14 days) and individual-delete buckets.
        now = datetime.now(timezone.utc)
        bulk_cutoff = now - _BULK_DELETE_CUTOFF
        bulk: list[discord.Message] = []
        single: list[discord.Message] = []
        for m in to_delete:
            if m.created_at >= bulk_cutoff:
                bulk.append(m)
            else:
                single.append(m)

        deleted = 0
        failed = 0

        # Bulk delete (chunked at 100; Discord's hard limit). Single-message
        # bulk_delete is fine, the API tolerates len==1 lists.
        for i in range(0, len(bulk), 100):
            chunk = bulk[i:i + 100]
            try:
                if len(chunk) == 1:
                    await chunk[0].delete()
                else:
                    await target_channel.delete_messages(chunk)
                deleted += len(chunk)
            except discord.Forbidden:
                failed += len(chunk)
                log.warning("bulk delete forbidden in #%s", target_channel.name)
                break  # no point retrying the rest
            except discord.HTTPException as e:
                log.warning("bulk delete HTTP error: %s", e)
                # Fall back to single-message deletes for this chunk
                single.extend(chunk)

        # Single deletes (older messages, plus any bulk-fallback). Slow path.
        for m in single:
            try:
                await m.delete()
                deleted += 1
            except discord.NotFound:
                deleted += 1  # already gone, count as success
            except discord.Forbidden:
                failed += 1
            except discord.HTTPException as e:
                failed += 1
                log.warning("single delete failed: %s", e)
            await asyncio.sleep(_SINGLE_DELETE_SLEEP)

        # Per-author breakdown (useful when only_author is None and the
        # purge swept a mixed bag).
        author_counts = Counter(
            (m.author.id, m.author.display_name) for m in to_delete[:deleted]
        )
        author_summary = ", ".join(
            f"{name} ×{c}" for (_uid, name), c in author_counts.most_common(5)
        )
        if len(author_counts) > 5:
            author_summary += f", +{len(author_counts) - 5} more"

        # ── Admin-facing confirmation. Goes to the log channel if one is
        #    configured (so a backstage purge stays backstage), otherwise
        #    inline. reply_permanent handles that routing for us.
        noun = "message" if deleted == 1 else "messages"
        lines = [
            f"🧹 **Purge** | {deleted} {noun} deleted in {target_channel.mention}",
        ]
        if only_author is not None:
            lines.append(f"👤 Target: {only_author.mention} (`{only_author.id}`)")
        elif author_summary:
            lines.append(f"👥 Authors: {author_summary}")
        lines.append(f"🛡️ By: {ctx.author.mention}")
        if failed:
            lines.append(f"⚠️ {failed} failed (permission/role hierarchy).")
        if skipped_protected:
            lines.append(
                f"🛑 Skipped protected user(s): "
                f"{', '.join(skipped_protected[:5])}"
                + (f", +{len(skipped_protected) - 5} more" if len(skipped_protected) > 5 else "")
            )
        await reply_permanent(ctx, "\n".join(lines))

        # reply_permanent already routes to CFG.log_channel_id when one is
        # configured AND it differs from the source channel. If the admin
        # ran the command IN the log channel, reply_permanent posted inline
        # there — same outcome. Either way: no separate _post_to_log call
        # needed. We already have a single audit message in the right place.

        # ── Persist to bot_actions DB for audit trail ──
        try:
            target_id = only_author.id if only_author else 0
            extra = {
                "target_channel_id": target_channel.id,
                "deleted": deleted,
                "failed": failed,
                "requested": n,
                "scope": "user" if only_author else "channel",
                "skipped_protected": skipped_protected,
                "ts": int(time.time()),
            }
            await STORE.log_bot_action(
                kind="purge",
                actor_id=ctx.author.id,
                target_id=target_id,
                channel_id=target_channel.id,
                summary=(
                    f"{deleted} msg(s) purged from #{target_channel.name}"
                    + (f" by {only_author.display_name}" if only_author else "")
                ),
                extra=json.dumps(extra),
            )
        except Exception as e:
            log.warning("purge DB log failed: %s", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PurgeCog(bot))
