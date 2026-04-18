"""
Role rule engine.

Reads config/role_rules.json and evaluates each enabled rule against
activity data in SQLite. When a rule's trigger condition is met:
  1. Grants the specified role (and removes any listed remove_roles)
  2. DMs the user with the notify.message (written in Clawy's voice)
  3. Optionally posts a public announcement in a specified channel
  4. Logs the grant to bot_actions and role_grants tables

The engine runs:
  - After every message (lightweight check — only if near a threshold)
  - On a periodic background task (every 10 minutes) for full evaluation

Rules are hot-reloadable via !roles reload without a bot restart.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from threading import Lock
from typing import Any

import discord
from discord.ext import commands, tasks

from core.config import CFG
from core.store import STORE

log = logging.getLogger(__name__)

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "role_rules.json"

# How often the full background sweep runs
_SWEEP_INTERVAL_MINUTES = 10

# Prune activity older than this on each sweep
_PRUNE_DAYS = 35


class RuleEngine:
    def __init__(self) -> None:
        self._lock = Lock()
        self._rules: list[dict[str, Any]] = []
        self.reload()

    def reload(self) -> int:
        with self._lock:
            try:
                raw = json.loads(RULES_PATH.read_text(encoding="utf-8"))
                self._rules = [r for r in raw.get("rules", []) if r.get("enabled", False)]
                log.info("Role rules loaded: %d enabled", len(self._rules))
                return len(self._rules)
            except Exception as e:
                log.error("Failed to load role_rules.json: %s", e)
                return 0

    def rules(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._rules)


RULE_ENGINE = RuleEngine()


# ── helpers ────────────────────────────────────────────────────────────

def _channel_id_for_name(guild: discord.Guild, name: str) -> int | None:
    ch = discord.utils.get(guild.text_channels, name=name)
    return ch.id if ch else None


async def _try_dm(user: discord.abc.User, text: str) -> None:
    try:
        await user.send(text[:1900])
    except (discord.Forbidden, discord.HTTPException) as e:
        log.debug("DM to %s failed: %s", user, e)


async def _apply_rule(
    rule: dict[str, Any],
    member: discord.Member,
    guild: discord.Guild,
) -> bool:
    """
    Apply one rule to one member.
    Returns True if the role was granted, False otherwise.
    """
    rule_id   = rule["id"]
    action    = rule.get("action", {})
    notify    = rule.get("notify", {})
    role_name = action.get("grant_role", "")

    # Already granted and once=True → skip
    if action.get("once", True):
        if await STORE.has_role_grant(member.id, rule_id):
            return False

    # Find the role
    role = discord.utils.get(guild.roles, name=role_name)
    if role is None:
        log.warning("Rule '%s': role '%s' not found in guild", rule_id, role_name)
        return False

    # Check bot can assign it
    if guild.me.top_role <= role:
        log.warning("Rule '%s': bot's top role is not above '%s'", rule_id, role_name)
        return False

    # Already has the role
    if role in member.roles:
        await STORE.set_role_grant(member.id, rule_id)  # mark so we don't keep checking
        return False

    # Grant role
    try:
        await member.add_roles(role, reason=f"Auto-role rule: {rule_id}")
    except discord.Forbidden:
        log.warning("Rule '%s': missing permission to grant '%s'", rule_id, role_name)
        return False

    # Remove specified roles (upgrades)
    for remove_name in action.get("remove_roles", []):
        remove_role = discord.utils.get(guild.roles, name=remove_name)
        if remove_role and remove_role in member.roles:
            try:
                await member.remove_roles(remove_role, reason=f"Role upgrade: {rule_id}")
            except discord.Forbidden:
                pass

    # Persist grant
    await STORE.set_role_grant(member.id, rule_id)
    await STORE.log_bot_action(
        kind="auto_role_grant",
        target_id=member.id,
        summary=f"rule '{rule_id}' → granted '{role_name}'",
    )

    log.info("Rule '%s': granted '%s' to %s", rule_id, role_name, member)

    # DM the user
    if notify.get("dm") and notify.get("message"):
        await _try_dm(member, notify["message"])

    # Optional public announcement
    ann_channel_id = notify.get("channel_id")
    if ann_channel_id:
        ch = guild.get_channel(int(ann_channel_id))
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(
                    f"🎭 {member.mention} has earned the **{role_name}** role.",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.DiscordException:
                pass

    # Log channel notice
    if CFG.log_channel_id:
        log_ch = guild.get_channel(CFG.log_channel_id)
        if isinstance(log_ch, discord.TextChannel):
            try:
                await log_ch.send(
                    f"🏅 **Auto-role granted**\n"
                    f"👤 {member.mention} (`{member.id}`)\n"
                    f"🎭 Role: **{role_name}**\n"
                    f"📋 Rule: `{rule_id}` — {rule.get('description', '')}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.DiscordException:
                pass

    return True


async def evaluate_member(member: discord.Member, guild: discord.Guild) -> None:
    """
    Evaluate all enabled rules against one member.
    Called after each message and during the periodic sweep.

    Trigger fields used:
      type          — "message_count" (only supported type)
      count         — messages needed in the window
      window_days   — rolling time window
      channel       — restrict to a specific channel name (null = server-wide)
      min_days_member   — user must have been in the server at least this many days
                          (uses Discord's joined_at — works even before the bot arrived)
      min_days_observed — user must have been seen by the bot for at least this many days
                          (uses users_seen.first_seen — ensures a track record with the bot)
    """
    import time as _time
    now = _time.time()

    for rule in RULE_ENGINE.rules():
        trigger = rule.get("trigger", {})
        rule_id = rule["id"]

        if trigger.get("type") != "message_count":
            continue

        count_needed = int(trigger.get("count", 0))
        window_days  = int(trigger.get("window_days", 30))
        channel_name = trigger.get("channel")
        min_days_member   = trigger.get("min_days_member")    # optional int
        min_days_observed = trigger.get("min_days_observed")  # optional int

        # ── Gate 1: min_days_member ──────────────────────────────────
        # Check how long they've been a member of the Discord server.
        # Uses discord.Member.joined_at (real Discord data, always accurate).
        if min_days_member is not None:
            if member.joined_at is None:
                log.debug("Rule '%s': skipping %s — joined_at unavailable", rule_id, member)
                continue
            days_in_server = (now - member.joined_at.timestamp()) / 86400
            if days_in_server < int(min_days_member):
                log.debug(
                    "Rule '%s': skipping %s — only %.1f days in server (need %s)",
                    rule_id, member, days_in_server, min_days_member
                )
                continue

        # ── Gate 2: min_days_observed ────────────────────────────────
        # Check how long the bot has been watching this user.
        # Uses users_seen.first_seen — only counts from when the bot started.
        if min_days_observed is not None:
            user_row = await STORE.get_user(member.id)
            if user_row is None:
                log.debug("Rule '%s': skipping %s — never seen before", rule_id, member)
                continue
            days_observed = (now - user_row["first_seen"]) / 86400
            if days_observed < int(min_days_observed):
                log.debug(
                    "Rule '%s': skipping %s — only observed %.1f days (need %s)",
                    rule_id, member, days_observed, min_days_observed
                )
                continue

        # ── Gate 3: message count in window ──────────────────────────
        window_seconds = window_days * 24 * 3600

        channel_id: int | None = None
        if channel_name:
            channel_id = _channel_id_for_name(guild, channel_name)
            if channel_id is None:
                log.debug("Rule '%s': channel '%s' not found", rule_id, channel_name)
                continue

        count = await STORE.count_activity(member.id, window_seconds, channel_id)

        if count >= count_needed:
            await _apply_rule(rule, member, guild)
        else:
            log.debug(
                "Rule '%s': %s has %d/%d messages in %dd window",
                rule_id, member, count, count_needed, window_days
            )


# ── Cog ────────────────────────────────────────────────────────────────

class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._sweep.start()

    def cog_unload(self) -> None:
        self._sweep.cancel()

    # ── periodic full sweep ───────────────────────────────────────────

    @tasks.loop(minutes=_SWEEP_INTERVAL_MINUTES)
    async def _sweep(self) -> None:
        """Evaluate all rules against all members in the guild."""
        if CFG.state.paused or CFG.state.sleeping:
            return
        await STORE.prune_activity(older_than_seconds=_PRUNE_DAYS * 24 * 3600)
        guild = self._guild()
        if guild is None:
            return
        for member in guild.members:
            if member.bot:
                continue
            try:
                await evaluate_member(member, guild)
            except Exception as e:
                log.warning("Sweep error for %s: %s", member, e)

    @_sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    # ── record activity on every message ─────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        if CFG.guild_id and message.guild.id != CFG.guild_id:
            return
        if message.channel.name in CFG.ignored_channels:
            return

        # Record the message in activity_log
        try:
            await STORE.record_activity(
                user_id=message.author.id,
                channel_id=message.channel.id,
                guild_id=message.guild.id,
            )
        except Exception as e:
            log.warning("record_activity failed: %s", e)
            return

        # Evaluate rules for this member (skip if no rules loaded)
        if not RULE_ENGINE.rules():
            return
        if not isinstance(message.author, discord.Member):
            return
        try:
            await evaluate_member(message.author, message.guild)
        except Exception as e:
            log.warning("evaluate_member failed: %s", e)

    # ── admin commands ────────────────────────────────────────────────

    def _is_admin(self, ctx: commands.Context) -> bool:
        if ctx.author.id == CFG.owner_id:
            return True
        if isinstance(ctx.author, discord.Member):
            return ctx.author.guild_permissions.administrator
        return False

    @commands.command(name="roles")
    async def roles_cmd(self, ctx: commands.Context, sub: str = "", *, arg: str = "") -> None:
        """
        Role rule management.
          !roles              — show loaded rules
          !roles reload       — reload role_rules.json from disk
          !roles check @user  — immediately evaluate all rules for a user
          !roles grants @user — show which rules have already fired for a user
          !roles reset @user <rule_id> — clear a grant so the rule can fire again
        """
        if not self._is_admin(ctx):
            return

        if not sub or sub == "list":
            rules = RULE_ENGINE.rules()
            if not rules:
                await ctx.reply("No enabled role rules loaded.")
                return
            lines = [f"**{len(rules)} active role rule(s):**"]
            for r in rules:
                t = r.get("trigger", {})
                a = r.get("action", {})
                lines.append(
                    f"• `{r['id']}` — {r.get('description', '')}\n"
                    f"  Trigger: **{t.get('count')} msgs** in **{t.get('window_days')}d**"
                    + (f" in #{t.get('channel')}" if t.get('channel') else " (server-wide)")
                    + f"\n  Grants: **{a.get('grant_role')}**"
                    + (f", removes: {a.get('remove_roles')}" if a.get('remove_roles') else "")
                )
            await ctx.reply("\n".join(lines)[:1900])

        elif sub == "reload":
            n = RULE_ENGINE.reload()
            await ctx.reply(f"Reloaded role rules. **{n}** rule(s) now active.")

        elif sub == "check":
            guild = self._guild()
            if guild is None:
                await ctx.reply("Guild not found.")
                return
            # Parse @mention from arg
            member = await self._resolve_member(ctx, arg)
            if member is None:
                await ctx.reply("Usage: `!roles check @user`")
                return
            await evaluate_member(member, guild)
            await ctx.reply(f"Evaluated rules for **{member.display_name}**.")

        elif sub == "grants":
            member = await self._resolve_member(ctx, arg)
            if member is None:
                await ctx.reply("Usage: `!roles grants @user`")
                return
            grants = await STORE.user_role_grants(member.id)
            if not grants:
                await ctx.reply(f"No role rules have fired for **{member.display_name}** yet.")
            else:
                await ctx.reply(
                    f"Rules already granted to **{member.display_name}**:\n"
                    + "\n".join(f"• `{g}`" for g in grants)
                )

        elif sub == "reset":
            parts = arg.strip().split()
            if len(parts) < 2:
                await ctx.reply("Usage: `!roles reset @user <rule_id>`")
                return
            member = await self._resolve_member(ctx, parts[0])
            rule_id = parts[-1]
            if member is None:
                await ctx.reply("Could not find that user.")
                return
            await STORE.clear_role_grant(member.id, rule_id)
            await ctx.reply(
                f"Cleared grant record for rule `{rule_id}` on **{member.display_name}**. "
                f"The rule will fire again if they re-qualify."
            )

        else:
            await ctx.reply(
                "Unknown subcommand. Options: `list`, `reload`, `check @user`, "
                "`grants @user`, `reset @user <rule_id>`"
            )

    # ── helpers ───────────────────────────────────────────────────────

    def _guild(self) -> discord.Guild | None:
        for g in self.bot.guilds:
            if CFG.guild_id == 0 or g.id == CFG.guild_id:
                return g
        return None

    async def _resolve_member(
        self, ctx: commands.Context, text: str
    ) -> discord.Member | None:
        # Try to parse a mention or ID from text or ctx.message.mentions
        if ctx.message.mentions:
            m = ctx.message.mentions[0]
            if isinstance(m, discord.Member):
                return m
        text = text.strip().strip("<@!>")
        if text.isdigit() and ctx.guild:
            try:
                return await ctx.guild.fetch_member(int(text))
            except discord.NotFound:
                pass
        return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RolesCog(bot))
