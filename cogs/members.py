"""Optional: greet new members / say goodbye via the LLM, in persona."""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.config import CFG
from core.ollama_client import OLLAMA
from core.persona import PERSONAS
from core.store import STORE

log = logging.getLogger(__name__)


class MembersCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _find_system_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        if guild.system_channel:
            return guild.system_channel
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                return ch
        return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if CFG.guild_id and member.guild.id != CFG.guild_id:
            return
        if CFG.state.paused:
            return
        channel = await self._find_system_channel(member.guild)
        if channel is None:
            return
        system = (
            f"{PERSONAS.render()}\n\n"
            "Write ONE short welcome line (<= 140 chars) for a new member joining the server. "
            "Return JSON: {\"message\": \"...\"}."
        )
        user = f"New member: {member.display_name}"
        result = await OLLAMA.generate_json(system, user)
        text = None
        if result and isinstance(result, dict):
            text = str(result.get("message", "")).strip()[:300]
        if not text:
            text = f"Welcome, {member.mention}."
        try:
            await channel.send(f"{member.mention} {text}")
            await STORE.log_bot_action(
                kind="welcome",
                target_id=member.id,
                channel_id=channel.id,
                summary=text[:200],
            )
        except discord.DiscordException as e:
            log.warning("welcome send failed: %s", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MembersCog(bot))
