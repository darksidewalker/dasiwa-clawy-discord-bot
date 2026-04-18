"""Entry point. Run with: python main.py"""
from __future__ import annotations

import asyncio
import logging
import signal

import discord
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
        command_prefix=CFG.command_prefix,
        intents=intents,
        help_command=None,
    )

    @bot.event
    async def on_ready() -> None:
        log.info("logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
        log.info("mode=%s", CFG.mode)
        healthy = await OLLAMA.health()
        if not healthy:
            log.warning("Ollama at %s is NOT reachable. Rule-based moderation only.",
                        CFG.ollama_url)
        else:
            log.info("Ollama reachable. Using model '%s'.", CFG.model)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.CommandNotFound):
            return
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
    await bot.load_extension("cogs.sleep")
    await bot.load_extension("cogs.roles")

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
