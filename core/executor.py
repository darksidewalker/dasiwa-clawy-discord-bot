"""
Execute a decided action with strict guardrails.

AUTONOMOUS ACTIONS (LLM can pick these):
    ignore, reply, warn, delete, timeout, assign_role, remove_role

MANUAL-ONLY ACTIONS (only callable from admin commands, not by the LLM):
    kick, ban, manual_mute

Every moderation action — including soft ones — is logged to the admin log
channel so the owner can review and decide if escalation is needed.

The LLM is intentionally NOT allowed to kick or ban. It flags problems.
The human decides on serious consequences.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import discord

from .config import CFG
from .expressions import react_to, send_with_extras
from .store import STORE

log = logging.getLogger(__name__)

# Discord's absolute limits
_DISCORD_MAX_TIMEOUT = 28 * 24 * 3600
_MIN_TIMEOUT = 60


def _is_protected(member: discord.Member) -> bool:
    if member.id == CFG.owner_id:
        return True
    if member.guild.owner_id == member.id:
        return True
    return any(r.name in CFG.protected_roles for r in member.roles)


async def _log_action(guild: discord.Guild, text: str) -> None:
    """Post to the configured admin log channel."""
    if not CFG.log_channel_id:
        return
    ch = guild.get_channel(CFG.log_channel_id)
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(text[:1900])
        except discord.DiscordException as e:
            log.warning("log send failed: %s", e)


def _strike_summary(strikes: int) -> str:
    if strikes == 0:
        return "no prior strikes"
    return f"**{strikes}** strike(s) in the last 24h"


# ============================================================
# PUBLIC: called from moderation cog for autonomous decisions
# ============================================================

async def execute(
    action: dict[str, Any],
    message: discord.Message,
) -> str:
    """
    Execute an autonomous moderation action decided by the LLM or prefilter.
    Kick and ban are blocked here — they must go through execute_manual().
    Returns a short summary string (for logging/debugging).
    """
    guild = message.guild
    if guild is None:
        return "no guild"

    if CFG.state.paused:
        return "paused"

    act = str(action.get("action", "ignore")).lower().strip()
    reason = str(action.get("reason", "")).strip()[:300]
    source = action.get("source", "llm")

    # Hard block: kick and ban are NEVER autonomous
    if act in {"kick", "ban"}:
        # The LLM wanted to kick/ban — log it as a flag for human review instead
        strikes = await STORE.count_strikes(message.author.id,
                                            CFG.mod.get("strike_window_hours", 24))
        await _log_action(
            guild,
            f"🚩 **FLAG for review** | #{message.channel.name}\n"
            f"👤 {message.author.mention} (`{message.author.id}`)\n"
            f"🤖 Clawy recommended **{act}** — {reason}\n"
            f"📊 {_strike_summary(strikes)}\n"
            f"💬 Message: `{message.content[:200]}`\n"
            f"Use `!kick @user` or `!ban @user <reason>` if you agree."
        )
        await STORE.log_mod_event(
            user_id=message.author.id, kind=f"flagged_for_{act}",
            reason=reason, source=source,
            channel_id=message.channel.id, message_id=message.id,
        )
        return f"flagged for {act} (human review required)"

    # Gate: action not in allowed list → ignore silently
    if act not in CFG.allowed_actions and act not in {"reply", "ignore"}:
        log.info("action %s not in allowed_actions — ignoring", act)
        return f"action '{act}' not allowed"

    author = message.author
    is_member = isinstance(author, discord.Member)

    try:
        # ── ignore ───────────────────────────────────────────────────
        if act == "ignore":
            # Even on "ignore", the LLM may have included a "react" field —
            # a non-verbal acknowledgment (e.g. 💀 on a savage burn). Apply
            # it best-effort; failure is silent.
            if CFG.expressions_enabled and CFG.expressions_allow_reactions:
                react_field = action.get("react")
                if react_field:
                    try:
                        await react_to(
                            message,
                            react_field,
                            cap=CFG.expressions_max_reactions,
                        )
                    except Exception as e:
                        log.debug("react-on-ignore failed: %s", e)
            return "ignored"

        # ── reply ────────────────────────────────────────────────────
        if act == "reply":
            text = str(action.get("message", "")).strip()[:1800]
            # Allow reply with no text if there's a sticker or attach to post —
            # send_with_extras handles that case. We only reject if there's
            # truly nothing to send.
            has_extras = bool(
                action.get("sticker") or action.get("attach") or action.get("react")
            )
            if not text and not has_extras:
                return "reply: empty message"
            sent = await send_with_extras(
                message.channel,
                text,
                action,
                cfg=CFG,
                reference=message,
                mention_author=False,
            )
            return "replied" if sent is not None else "reply: nothing sent"

        # Protection check for all punitive actions
        if is_member and _is_protected(author):
            log.info("refused %s: %s is protected", act, author)
            return f"refused {act}: author is protected"

        # ── delete ───────────────────────────────────────────────────
        if act == "delete":
            channel_name = getattr(message.channel, "name", "the channel")
            try:
                await message.delete()
            except discord.Forbidden:
                return "delete: missing permission"
            except discord.NotFound:
                pass

            # User-facing notifications (DM + short channel notice), gated by
            # config.notify_user.* — see config.yaml.
            if CFG.notify_user_enabled:
                if CFG.notify_user_dm:
                    try:
                        await author.send(
                            f"Your message in #{channel_name} was removed by moderation."
                            + (f"\nReason: {reason}" if reason else "")
                        )
                    except discord.DiscordException:
                        # User has DMs disabled or blocked — silent skip.
                        pass

                if CFG.notify_user_channel_notice:
                    try:
                        await message.channel.send(
                            f"{author.mention} Your message was removed by moderation.",
                            allowed_mentions=discord.AllowedMentions(users=True),
                            delete_after=CFG.notify_user_notice_seconds,
                        )
                    except discord.DiscordException:
                        pass

            strikes = await STORE.count_strikes(author.id,
                                                CFG.mod.get("strike_window_hours", 24))
            await STORE.log_mod_event(
                user_id=author.id, kind="delete", reason=reason, source=source,
                channel_id=message.channel.id, message_id=message.id,
            )
            await _log_action(
                guild,
                f"🗑️ **Deleted message** | #{message.channel.name}\n"
                f"👤 {author.mention} (`{author.id}`)\n"
                f"📋 Reason: {reason}\n"
                f"📊 {_strike_summary(strikes)}"
            )
            return "deleted"

        # ── warn ─────────────────────────────────────────────────────
        if act == "warn":
            text = str(action.get("message", "")).strip() or f"Watch yourself. {reason}"
            text = text[:1800]
            # Prepend mention to the message body (send_with_extras passes
            # content through as-is, including any leading mention).
            await send_with_extras(
                message.channel,
                f"{author.mention} {text}",
                action,
                cfg=CFG,
                reference=message,
                mention_author=False,
            )
            await STORE.log_mod_event(
                user_id=author.id, kind="warn", reason=reason, source=source,
                channel_id=message.channel.id, message_id=message.id,
            )
            strikes = await STORE.count_strikes(author.id,
                                                CFG.mod.get("strike_window_hours", 24))
            await _log_action(
                guild,
                f"⚠️ **Warning issued** | #{message.channel.name}\n"
                f"👤 {author.mention} (`{author.id}`)\n"
                f"📋 Reason: {reason}\n"
                f"📊 {_strike_summary(strikes)}"
            )
            return "warned"

        # ── timeout (autonomous — capped) ────────────────────────────
        if act == "timeout":
            if not is_member:
                return "timeout: author not a member"

            requested = int(
                action.get("duration_seconds")
                or CFG.mod.get("default_timeout_seconds", 600)
            )
            # Cap to the configured autonomous maximum
            cap = CFG.max_autonomous_timeout_seconds
            dur = max(_MIN_TIMEOUT, min(requested, cap))
            capped = dur < requested  # was it reduced?

            until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=dur)
            try:
                await author.timeout(until, reason=reason[:500])
            except discord.Forbidden:
                return "timeout: missing permission / role hierarchy"

            if action.get("also_delete"):
                try:
                    await message.delete()
                except discord.DiscordException:
                    pass

            # Optional DM to the user (used by blocklist hits)
            if action.get("notify_user") and action.get("notify_message"):
                try:
                    await author.send(str(action["notify_message"])[:1800])
                except discord.DiscordException:
                    pass  # user has DMs disabled — that's fine

            await STORE.log_mod_event(
                user_id=author.id, kind="timeout", reason=reason, source=source,
                channel_id=message.channel.id, message_id=message.id,
                extra=f'{{"duration_seconds": {dur}, "requested": {requested}}}',
            )
            strikes = await STORE.count_strikes(author.id,
                                                CFG.mod.get("strike_window_hours", 24))
            cap_note = f" *(capped from {requested}s)*" if capped else ""
            await _log_action(
                guild,
                f"🔇 **Muted** | #{message.channel.name}\n"
                f"👤 {author.mention} (`{author.id}`)\n"
                f"⏱️ Duration: {dur // 60} min{cap_note}\n"
                f"📋 Reason: {reason}\n"
                f"📊 {_strike_summary(strikes)}\n"
                f"💡 Use `!kick @user <reason>` or `!ban @user <reason>` if needed."
            )
            return f"timed out {dur}s"

        # ── assign_role / remove_role ────────────────────────────────
        if act in {"assign_role", "remove_role"}:
            if not is_member:
                return f"{act}: author not a member"
            role_name = str(action.get("role", "")).strip()
            if not role_name:
                return f"{act}: no role specified"
            role = discord.utils.get(guild.roles, name=role_name)
            if role is None:
                return f"{act}: role '{role_name}' not found"
            if role.name in CFG.protected_roles:
                return f"{act}: role '{role_name}' is protected"
            if guild.me.top_role <= role:
                return f"{act}: my role is too low"
            try:
                if act == "assign_role":
                    await author.add_roles(role, reason=reason[:500])
                else:
                    await author.remove_roles(role, reason=reason[:500])
            except discord.Forbidden:
                return f"{act}: missing permission"
            await STORE.log_bot_action(
                kind=act, target_id=author.id,
                channel_id=message.channel.id,
                summary=f"{act} '{role_name}' — {reason}",
            )
            await _log_action(
                guild,
                f"🏷️ **Role change** | {act}\n"
                f"👤 {author.mention} (`{author.id}`)\n"
                f"🎭 Role: {role_name}\n"
                f"📋 Reason: {reason}"
            )
            return f"{act} {role_name}"

        return f"unknown action: {act}"

    except Exception as e:
        log.exception("execute failed")
        return f"error: {e}"


# ============================================================
# MANUAL: called only from admin commands (!kick, !ban, !mute)
# ============================================================

async def execute_kick(
    guild: discord.Guild,
    member: discord.Member,
    reason: str,
    actor_id: int,
) -> str:
    if _is_protected(member):
        return "refused: member is protected"
    try:
        await member.kick(reason=reason[:500])
    except discord.Forbidden:
        return "kick: missing permission"
    await STORE.log_mod_event(
        user_id=member.id, kind="kick", reason=reason, source="admin",
    )
    await STORE.log_bot_action(
        kind="kick", actor_id=actor_id, target_id=member.id,
        summary=reason,
    )
    await _log_action(
        guild,
        f"👢 **Kicked** (manual)\n"
        f"👤 {member.mention} (`{member.id}`)\n"
        f"📋 Reason: {reason}\n"
        f"🛡️ By: <@{actor_id}>"
    )
    return "kicked"


async def execute_ban(
    guild: discord.Guild,
    member: discord.Member,
    reason: str,
    actor_id: int,
    delete_days: int = 1,
) -> str:
    if _is_protected(member):
        return "refused: member is protected"
    try:
        await member.ban(reason=reason[:500], delete_message_days=delete_days)
    except discord.Forbidden:
        return "ban: missing permission"
    await STORE.log_mod_event(
        user_id=member.id, kind="ban", reason=reason, source="admin",
    )
    await STORE.log_bot_action(
        kind="ban", actor_id=actor_id, target_id=member.id,
        summary=reason,
    )
    await _log_action(
        guild,
        f"🔨 **Banned** (manual)\n"
        f"👤 {member.mention} (`{member.id}`)\n"
        f"📋 Reason: {reason}\n"
        f"🛡️ By: <@{actor_id}>"
    )
    return "banned"


async def execute_mute(
    guild: discord.Guild,
    member: discord.Member,
    duration_seconds: int,
    reason: str,
    actor_id: int,
) -> str:
    if _is_protected(member):
        return "refused: member is protected"
    dur = max(_MIN_TIMEOUT, min(duration_seconds, _DISCORD_MAX_TIMEOUT))
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=dur)
    try:
        await member.timeout(until, reason=reason[:500])
    except discord.Forbidden:
        return "mute: missing permission / role hierarchy"
    await STORE.log_mod_event(
        user_id=member.id, kind="timeout", reason=reason, source="admin",
        extra=f'{{"duration_seconds": {dur}}}',
    )
    await _log_action(
        guild,
        f"🔇 **Muted** (manual)\n"
        f"👤 {member.mention} (`{member.id}`)\n"
        f"⏱️ Duration: {dur // 60} min\n"
        f"📋 Reason: {reason}\n"
        f"🛡️ By: <@{actor_id}>"
    )
    return f"muted {dur // 60} min"
