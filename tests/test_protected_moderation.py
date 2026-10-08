import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cogs.moderation import ModerationCog
from core import executor
from test_honeypot import config, message


@pytest.mark.parametrize('case', ['configured_owner', 'guild_owner', 'protected_role', 'self', 'ordinary'])
@pytest.mark.parametrize('verdict', ['warn', 'timeout'])
def test_mention_spam_never_punishes_protected_members(case, verdict):
    msg, cfg = message(), config()
    cfg.raw['mode'] = 'moderate_only'
    msg.channel.id = 13
    if case == 'configured_owner': msg.author.id = cfg.owner_id
    elif case == 'guild_owner': msg.author.id = msg.guild.owner_id
    elif case == 'protected_role': msg.author.roles = [SimpleNamespace(name='Admin')]
    elif case == 'self':
        msg.author.id = msg.guild.me.id
        msg.author.bot = True
    msg.author.timeout = AsyncMock()
    bot_user = SimpleNamespace(id=99, name='Clawy', display_name='Clawy')
    msg.mentions = [bot_user]
    msg.channel.send = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
    cog = ModerationCog(SimpleNamespace(user=bot_user))
    with patch('cogs.moderation.CFG', cfg), patch.object(executor, 'CFG', cfg), \
         patch.object(cog, '_touch_and_mine', AsyncMock()), \
         patch.object(cog, '_addresses_bot', return_value=False), \
         patch('cogs.moderation.prefilter', AsyncMock(return_value=('skip', None))), \
         patch('cogs.moderation.vision_enabled', return_value=False), \
         patch('cogs.moderation.MENTION_RL.record') as record, \
         patch('cogs.moderation.MENTION_RL.check', return_value=verdict), \
         patch('cogs.moderation.MENTION_RL.timeout_duration', return_value=300), \
         patch('cogs.moderation.asyncio.sleep', AsyncMock()), \
         patch('cogs.moderation.STORE.log_mod_event', AsyncMock()) as event:
        asyncio.run(cog.on_message(msg))
    if case == 'ordinary':
        record.assert_called_once_with(msg.author.id)
        msg.channel.send.assert_awaited_once()
        event.assert_awaited_once()
        if verdict == 'timeout': msg.author.timeout.assert_awaited_once()
        else: msg.author.timeout.assert_not_awaited()
    else:
        msg.author.timeout.assert_not_awaited()
        msg.channel.send.assert_not_awaited()
        event.assert_not_awaited()
        record.assert_not_called()


@pytest.mark.parametrize('case', ['configured_owner', 'guild_owner', 'protected_role', 'self'])
@pytest.mark.parametrize('action', ['ban', 'kick', 'mute', 'timeout', 'honeypot'])
def test_all_punitive_executors_refuse_protected_members(case, action):
    msg, cfg = message(), config()
    cfg.raw['allowed_actions'] = ['timeout']
    if case == 'configured_owner': msg.author.id = cfg.owner_id
    elif case == 'guild_owner': msg.author.id = msg.guild.owner_id
    elif case == 'protected_role': msg.author.roles = [SimpleNamespace(name='Admin')]
    else: msg.author.id = msg.guild.me.id
    msg.author.kick = AsyncMock()
    msg.author.timeout = AsyncMock()
    with patch.object(executor, 'CFG', cfg), \
         patch.object(executor.STORE, 'log_mod_event', AsyncMock()) as event:
        if action == 'ban': coro = executor.execute_ban(msg.guild, msg.author, 'test', 3)
        elif action == 'kick': coro = executor.execute_kick(msg.guild, msg.author, 'test', 3)
        elif action == 'mute': coro = executor.execute_mute(msg.guild, msg.author, 300, 'test', 3)
        elif action == 'timeout': coro = executor.execute({'action': 'timeout'}, msg)
        else: coro = executor.execute_honeypot(msg)
        result = asyncio.run(coro)
    assert result.startswith('refused')
    msg.author.ban.assert_not_awaited()
    msg.author.kick.assert_not_awaited()
    msg.author.timeout.assert_not_awaited()
    msg.delete.assert_not_awaited()
    event.assert_not_awaited()
