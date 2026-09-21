"""Typed Discord application commands backed by existing command handlers."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.config import CFG
from ._common import _resolve_mod_roles, is_admin_member, is_mod_member


class MoveDestinationView(discord.ui.View):
    def __init__(self, slash: "SlashCog", message: discord.Message) -> None:
        super().__init__(timeout=60)
        self.slash = slash
        self.message = message

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Destination channel",
        channel_types=[discord.ChannelType.text],
    )
    async def destination(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        context = await self.slash._context(interaction, admin=False)
        if context is None:
            return
        target = select.values[0]
        move = self.slash.bot.get_cog("MoveCog")
        if move is None or not isinstance(target, discord.TextChannel):
            await interaction.followup.send("Move command unavailable.", ephemeral=True)
            return
        await move._perform_move(context, [self.message], target, self.message.author)
        self.stop()


class SlashCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _context(
        self, interaction: discord.Interaction, *, admin: bool
    ) -> commands.Context | None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await interaction.response.send_message("Server command only.", ephemeral=True)
            return None
        if admin:
            authorized = is_admin_member(interaction.user, owner_id=CFG.owner_id)
        else:
            roles = _resolve_mod_roles(interaction.guild)
            authorized = is_mod_member(
                interaction.user, owner_id=CFG.owner_id, mod_role_ids=roles
            )
        if not authorized:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return None
        await interaction.response.defer(ephemeral=True)
        return await commands.Context.from_interaction(interaction)

    async def _run(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *args: object,
        admin: bool = False,
        **kwargs: object,
    ) -> None:
        context = await self._context(interaction, admin=admin)
        if context is None:
            return
        command = self.bot.get_command(command_name)
        if command is None or command.cog is None:
            await interaction.followup.send("Command unavailable.", ephemeral=True)
            return
        await command.callback(command.cog, context, *args, **kwargs)

    @app_commands.command(description="Pause autonomous actions.")
    async def pause(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "pause", admin=True)

    @app_commands.command(description="Resume autonomous actions.")
    async def resume(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "resume", admin=True)

    @app_commands.command(description="Set bot mode for this session.")
    @app_commands.describe(mode="New bot mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Moderate only", value="moderate_only"),
        app_commands.Choice(name="Chat and moderate", value="chat_and_moderate"),
        app_commands.Choice(name="Chat only", value="chat_only"),
    ])
    async def mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await self._run(interaction, "mode", mode.value, admin=True)

    @app_commands.command(description="Switch persona.")
    async def persona(self, interaction: discord.Interaction, key: str) -> None:
        await self._run(interaction, "persona", key, admin=True)

    @app_commands.command(description="Switch persona mood.")
    async def mood(self, interaction: discord.Interaction, name: str) -> None:
        await self._run(interaction, "mood", name, admin=True)

    @app_commands.command(description="Set Ollama model for this session.")
    async def model(self, interaction: discord.Interaction, name: str) -> None:
        await self._run(interaction, "model", name, admin=True)

    @app_commands.command(description="Set model thinking for this session.")
    @app_commands.choices(setting=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Use config default", value="reset"),
    ])
    async def think(self, interaction: discord.Interaction, setting: app_commands.Choice[str]) -> None:
        await self._run(interaction, "think", setting.value, admin=True)

    @app_commands.command(name="diagnostics", description="Show Clawy health diagnostics.")
    async def diagnostics(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "diag", admin=True)

    @app_commands.command(description="Show moderation strikes for a member.")
    async def strikes(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._run(interaction, "strikes", member, admin=True)

    @app_commands.command(description="Show stored profile for a member.")
    async def whois(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._run(interaction, "whois", member, admin=True)

    @app_commands.command(description="Kick a member.")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given.") -> None:
        await self._run(interaction, "kick", member, reason=reason, admin=False)

    @app_commands.command(description="Ban a member.")
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given.") -> None:
        await self._run(interaction, "ban", member, reason=reason, admin=False)

    @app_commands.command(description="Timeout a member.")
    @app_commands.describe(duration="Examples: 30m, 2h, 1h30m")
    async def mute(self, interaction: discord.Interaction, member: discord.Member, duration: str = "10m", reason: str = "No reason given.") -> None:
        await self._run(interaction, "mute", member, duration, reason=reason, admin=False)

    @app_commands.command(description="Remove a member timeout.")
    async def unmute(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._run(interaction, "unmute", member, admin=False)

    @app_commands.command(description="Purge recent messages from a channel.")
    async def purge(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
        count: app_commands.Range[int, 1, 100], member: discord.Member | None = None,
    ) -> None:
        await self._run(
            interaction, "purge", channel, min(count, CFG.move_max_batch), member, admin=False
        )

    @app_commands.command(name="purge-user", description="Purge a member's recent messages.")
    async def purge_user(
        self, interaction: discord.Interaction, member: discord.Member,
        count: app_commands.Range[int, 1, 100], channel: discord.TextChannel | None = None,
    ) -> None:
        await self._run(
            interaction, "purgeuser", member, min(count, CFG.move_max_batch), channel, admin=False
        )

    @app_commands.command(name="move-last", description="Move a member's recent messages.")
    async def move_last(
        self, interaction: discord.Interaction, member: discord.Member,
        count: app_commands.Range[int, 1, 100], destination: discord.TextChannel,
    ) -> None:
        await self._run(
            interaction, "movelast", member, min(count, CFG.move_max_batch), destination, admin=False
        )

    @app_commands.command(description="Put Clawy to sleep.")
    async def sleep(self, interaction: discord.Interaction, duration: str = "") -> None:
        await self._run(interaction, "sleep", duration, admin=True)

    @app_commands.command(description="Wake Clawy.")
    async def wake(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "wake", admin=True)

    @app_commands.command(description="List or reload activity role rules.")
    @app_commands.choices(action=[
        app_commands.Choice(name="List", value="list"),
        app_commands.Choice(name="Reload", value="reload"),
    ])
    async def roles(self, interaction: discord.Interaction, action: app_commands.Choice[str]) -> None:
        await self._run(interaction, "roles", action.value, admin=True)

    @app_commands.command(name="role-check", description="Evaluate role rules for a member.")
    async def role_check(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._run(interaction, "roles", "check", arg=member.mention, admin=True)

    @app_commands.command(name="role-grants", description="Show role rules already granted to a member.")
    async def role_grants(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._run(interaction, "roles", "grants", arg=member.mention, admin=True)

    @app_commands.command(name="role-reset", description="Clear one role-rule grant for a member.")
    async def role_reset(
        self, interaction: discord.Interaction, member: discord.Member, rule_id: str
    ) -> None:
        await self._run(interaction, "roles", "reset", arg=f"{member.mention} {rule_id}", admin=True)

    @app_commands.command(description="Manage quiet hours.")
    @app_commands.choices(action=[
        app_commands.Choice(name="Show", value="show"),
        app_commands.Choice(name="Enable", value="on"),
        app_commands.Choice(name="Disable", value="off"),
        app_commands.Choice(name="Reset", value="reset"),
    ])
    async def quiet(self, interaction: discord.Interaction, action: app_commands.Choice[str]) -> None:
        await self._run(interaction, "quiet", "" if action.value == "show" else action.value, admin=True)

    @app_commands.command(name="chat-roles", description="Manage chat role allowlist.")
    @app_commands.choices(action=[
        app_commands.Choice(name="Show", value="show"),
        app_commands.Choice(name="Add", value="add"),
        app_commands.Choice(name="Remove", value="remove"),
        app_commands.Choice(name="Clear", value="clear"),
        app_commands.Choice(name="Reset", value="reset"),
    ])
    async def chat_roles(
        self, interaction: discord.Interaction, action: app_commands.Choice[str], role: discord.Role | None = None
    ) -> None:
        await self._run(
            interaction, "chatroles", "" if action.value == "show" else action.value,
            rolename=role.name if role else "", admin=True,
        )

    @app_commands.command(description="Set proactive reply chance (0 disables).")
    async def proactive(self, interaction: discord.Interaction, chance: app_commands.Range[float, 0, 1]) -> None:
        await self._run(interaction, "proactive", str(chance), admin=True)

    async def delete_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        context = await self._context(interaction, admin=False)
        if context is None:
            return
        purge = self.bot.get_cog("PurgeCog")
        if purge is None or not isinstance(message.channel, discord.TextChannel):
            await interaction.followup.send("Delete command unavailable.", ephemeral=True)
            return
        await purge._do_purge(
            context, target_channel=message.channel, n=1, only_author=None,
            explicit_targets=[message], force_notify=True,
        )

    async def move_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        context = await self._context(interaction, admin=False)
        if context is None:
            return
        if not isinstance(message.channel, discord.TextChannel):
            await interaction.followup.send("Move command only works in text channels.", ephemeral=True)
            return
        await interaction.followup.send(
            "Choose destination channel.", ephemeral=True,
            view=MoveDestinationView(self, message),
        )


async def setup(bot: commands.Bot) -> None:
    slash = SlashCog(bot)
    await bot.add_cog(slash)
    bot.tree.add_command(app_commands.ContextMenu(name="Delete message", callback=slash.delete_message))
    bot.tree.add_command(app_commands.ContextMenu(name="Move message", callback=slash.move_message))
