import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import CFG
from main import build_bot


def test_guild_sync_contains_commands_and_removes_stale_globals(monkeypatch):
    monkeypatch.setitem(CFG.raw, "guild_id", 123)

    async def run():
        bot = build_bot()
        await bot.load_extension("cogs.slash")
        published = []

        async def sync(*, guild=None):
            published.append((getattr(guild, "id", None),
                              {cmd.name for cmd in bot.tree.get_commands(guild=guild)}))
            return []

        try:
            with patch.object(bot.tree, "sync", side_effect=sync), \
                 patch("main.OLLAMA.health", AsyncMock(return_value=False)):
                await bot.on_ready()
                await bot.on_ready()
            assert len(published) == 2
            assert published[0] == (None, set())
            assert published[1][0] == 123
            assert {"pause", "react", "Clawy react to this"} <= published[1][1]
        finally:
            await bot.close()
    asyncio.run(run())


def test_global_sync_preserves_commands(monkeypatch):
    monkeypatch.setitem(CFG.raw, "guild_id", 0)

    async def run():
        bot = build_bot()
        await bot.load_extension("cogs.slash")
        try:
            sync = AsyncMock(return_value=[])
            with patch.object(bot.tree, "sync", sync), \
                 patch("main.OLLAMA.health", AsyncMock(return_value=False)):
                await bot.on_ready()
            sync.assert_awaited_once_with()
            assert "pause" in {cmd.name for cmd in bot.tree.get_commands()}
        finally:
            await bot.close()
    asyncio.run(run())
