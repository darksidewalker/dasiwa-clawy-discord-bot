import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cogs.slash import SlashCog


@pytest.mark.parametrize("method, action", [("react_to_message", "react"), ("analyze_message", "analyze")])
def test_context_media_fetches_thread_message(method, action):
    thread = Mock(spec=discord.Thread)
    full = SimpleNamespace(id=12, attachments=[SimpleNamespace(filename="picture.png")], embeds=[])
    thread.fetch_message = AsyncMock(return_value=full)
    interaction = SimpleNamespace(
        channel=thread, channel_id=34, guild=SimpleNamespace(get_channel=Mock(return_value=None)),
        response=SimpleNamespace(send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    partial = SimpleNamespace(id=12, attachments=[], embeds=[])
    cog = SlashCog(SimpleNamespace())
    handler = AsyncMock()
    with patch.object(cog, "_context", AsyncMock(return_value=SimpleNamespace())), \
         patch.object(cog, "_media_action_on", handler):
        asyncio.run(getattr(cog, method)(interaction, partial))
    thread.fetch_message.assert_awaited_once_with(12)
    assert handler.await_count == 1
    assert handler.call_args.args[:3] == (interaction, full, action)


def test_context_media_missing_guild_does_not_fetch():
    interaction = SimpleNamespace(guild=None, response=SimpleNamespace(send_message=AsyncMock()))
    cog = SlashCog(SimpleNamespace())
    with patch.object(cog, "_context", AsyncMock(return_value=None)) as authorize:
        asyncio.run(cog.react_to_message(interaction, SimpleNamespace(id=12)))
    authorize.assert_awaited_once_with(interaction, admin=True)


@pytest.mark.parametrize("filename, expected", [("cat.PNG", True), ("cat.webp", True), ("movie.mp4", False), ("no_extension", False)])
def test_slash_media_requires_image_extension(filename, expected):
    cog = SlashCog(SimpleNamespace())
    assert cog._has_media(SimpleNamespace(attachments=[SimpleNamespace(filename=filename)], embeds=[])) is expected
