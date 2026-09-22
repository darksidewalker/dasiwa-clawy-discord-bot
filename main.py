"""Entry point. Run with: python main.py"""
from __future__ import annotations

import asyncio
import logging
import signal

import discord
from discord import app_commands
from discord.ext import commands

from core.config import CFG
from core.ollama_client import OLLAMA
from core.store import STORE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True   # privileged — read message text
    intents.members = True           # privileged — see member joins/profile
    intents.presences = True         # privileged — read online/idle/offline status
    intents.guilds = True

    bot = commands.Bot(
        command_prefix=CFG.command_prefix or (),
        intents=intents,
        help_command=None,
    )

    synced_commands = False

    @bot.event
    async def on_ready() -> None:
        nonlocal synced_commands
        log.info("logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
        if not synced_commands:
            if CFG.guild_id:
                # Publish only guild commands. Registering the same commands both
                # globally and in the guild makes Discord show each one twice.
                # First clear old global registrations, then install this local
                # tree in the configured guild for immediate availability.
                scope = discord.Object(id=CFG.guild_id)
                registered = list(bot.tree.get_commands())
                bot.tree.clear_commands(guild=None)
                await bot.tree.sync()
                for command in registered:
                    bot.tree.add_command(command, guild=scope)
                synced = await bot.tree.sync(guild=scope)
                log.info("synced %d application commands for guild", len(synced))
            else:
                synced = await bot.tree.sync()
                log.info("synced %d application commands globally", len(synced))
            synced_commands = True
        log.info("mode=%s", CFG.mode)
        healthy = await OLLAMA.health()
        if not healthy:
            log.warning("Ollama at %s is NOT reachable. Rule-based moderation only.",
                        CFG.ollama_url)
        else:
            log.info("Ollama reachable. Using model '%s'.", CFG.model)

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandNotFound):
            await interaction.response.send_message(
                "This stale command was removed. Use Clawy's current command list.",
                ephemeral=True,
            )
            return
        log.exception("application command failed", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Command failed. Check bot logs.", ephemeral=True)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        # Commands the user typed that don't exist — silently ignore.
        if isinstance(error, commands.CommandNotFound):
            return

        # Argument parsing errors (bad user input, missing arg, unresolvable
        # member/channel/etc.) happen BEFORE cog_before_invoke runs, so our
        # CleanCommandCog hasn't deleted the !command yet. Do it here, and
        # send a transient hint so the admin sees what went wrong.
        from cogs._common import CleanCommandCog, ack, delete_cmd

        is_clean_cog = isinstance(ctx.cog, CleanCommandCog)

        if isinstance(error, commands.UserInputError):
            if is_clean_cog:
                await delete_cmd(ctx)
                # Show a short hint — use the original exception's message
                # (usually: "Member 'X' not found", "Missing required argument",
                # "Converting to 'int' failed").
                await ack(ctx, f"⚠️ {type(error).__name__}: {error}")
            else:
                log.warning("parse error: %s", error)
            return

        # CheckFailure = non-admin hit a gated command. cog_check already
        # deleted the message via CleanCommandCog, so just stay silent.
        if isinstance(error, commands.CheckFailure):
            return

        # Anything else — log and, if it came from a clean cog, clean up.
        if is_clean_cog:
            await delete_cmd(ctx)
            await ack(ctx, f"⚠️ command failed: {type(error).__name__}")
        log.warning("command error: %s", error)

    return bot


async def _main() -> None:
    # Point the STORE at the path from config, then init it
    STORE.path = CFG.db_path
    await STORE.init()

    bot = build_bot()
    await bot.load_extension("cogs.moderation")
    await bot.load_extension("cogs.admin")
    await bot.load_extension("cogs.members")
    await bot.load_extension("cogs.move")
    await bot.load_extension("cogs.purge")
    await bot.load_extension("cogs.sleep")
    await bot.load_extension("cogs.roles")
    await bot.load_extension("cogs.slash")

    stop = asyncio.Event()

    def _handle_signal() -> None:
        log.info("signal received, shutting down")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

    try:
        async with bot:
            bot_task = asyncio.create_task(bot.start(CFG.discord_token))
            stop_task = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait(
                {bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                await bot.close()
            for t in done:
                if t.exception():
                    raise t.exception()  # type: ignore[misc]
    finally:
        await OLLAMA.close()
        await STORE.close()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
