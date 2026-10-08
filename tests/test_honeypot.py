import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import Config
from core import executor


def message():
    guild = SimpleNamespace(id=1, owner_id=2, me=SimpleNamespace(
        id=99, guild_permissions=SimpleNamespace(ban_members=True), top_role=10))
    member = MagicMock(spec=discord.Member)
    member.id = 7
    member.bot = False
    member.guild = guild
    member.roles = []
    member.top_role = 1
    member.mention = '<@7>'
    member.ban = AsyncMock()
    return SimpleNamespace(id=100, guild=guild, author=member,
                           channel=SimpleNamespace(id=12, name='honeypot'), content='hello',
                           delete=AsyncMock())


def config():
    return Config(raw={'guild_id': 1, 'owner_id': 3, 'protected_roles': ['Admin'],
                       'moderation': {'honeypot_enabled': True, 'honeypot_channel_id': 12}})


def test_honeypot_post_bans_and_audits_without_deleting_other_messages():
    msg = message()
    event = AsyncMock()
    audit = AsyncMock()
    with patch.object(executor, 'CFG', config()), \
         patch.object(executor.STORE, 'log_mod_event', event), \
         patch.object(executor, '_log_action', audit):
        result = asyncio.run(executor.execute_honeypot(msg))
    assert result == 'banned'
    msg.delete.assert_awaited_once()
    msg.author.ban.assert_awaited_once()
    assert msg.author.ban.await_args.kwargs['delete_message_seconds'] == 0
    assert event.await_args.kwargs['kind'] == 'ban'
    assert event.await_args.kwargs['source'] == 'honeypot'
    assert event.await_args.kwargs['message_id'] == 100
    audit.assert_awaited_once()


@pytest.mark.parametrize("case", ["configured_owner", "guild_owner", "role", "paused",
    "sleeping", "chat_only", "disabled", "unset", "other_channel", "other_guild",
    "self", "own_bot", "protected_bot", "non_member", "dm"])
def test_honeypot_exemptions_never_ban(case):
    msg, cfg = message(), config()
    member = msg.author
    if case == "configured_owner": member.id = 3
    elif case == "guild_owner": member.id = 2
    elif case == "role": member.roles = [SimpleNamespace(name='Admin')]
    elif case == "paused": cfg.state.paused = True
    elif case == "sleeping": cfg.state.sleeping = True
    elif case == "chat_only": cfg.raw['mode'] = 'chat_only'
    elif case == "disabled": cfg.mod['honeypot_enabled'] = False
    elif case == "unset": cfg.raw.pop('moderation')
    elif case == "other_channel": msg.channel.id = 13
    elif case == "other_guild": msg.guild.id = 4
    elif case == "self": member.id = msg.guild.me.id
    elif case == "own_bot":
        member.id = msg.guild.me.id
        member.bot = True
    elif case == "protected_bot":
        member.bot = True
        member.roles = [SimpleNamespace(name='Admin')]
    elif case == "non_member": msg.author = SimpleNamespace(id=7, bot=False)
    elif case == "dm": msg.guild = None
    with patch.object(executor, 'CFG', cfg), \
         patch.object(executor.STORE, 'log_mod_event', AsyncMock()) as event:
        asyncio.run(executor.execute_honeypot(msg))
    member.ban.assert_not_awaited()
    event.assert_not_awaited()
    msg.delete.assert_not_awaited()


@pytest.mark.parametrize("case", ['permission', 'hierarchy', 'forbidden', 'http'])
def test_honeypot_ban_failures_are_audited(case):
    msg = message()
    if case == 'permission': msg.guild.me.guild_permissions.ban_members = False
    elif case == 'hierarchy': msg.author.top_role = 10
    else:
        response = SimpleNamespace(status=403 if case == 'forbidden' else 500, reason='failure')
        exception = discord.Forbidden if case == 'forbidden' else discord.HTTPException
        msg.author.ban.side_effect = exception(response, 'failure')
    with patch.object(executor, 'CFG', config()), \
         patch.object(executor.STORE, 'log_mod_event', AsyncMock()) as event, \
         patch.object(executor, '_log_action', AsyncMock()) as audit:
        result = asyncio.run(executor.execute_honeypot(msg))
    assert result.startswith('ban failed:')
    assert event.await_args.kwargs['kind'] == 'honeypot_ban_failed'
    audit.assert_awaited_once()
    if case in {'permission', 'hierarchy'}:
        msg.author.ban.assert_not_awaited()


@pytest.mark.parametrize('prefix', ['', '!'])
@pytest.mark.parametrize('content', ['hello', '', '!help'])
def test_listener_routes_honeypot_before_commands_ignored_channels_and_chat(content, prefix):
    from cogs.moderation import ModerationCog
    msg, cfg = message(), config()
    msg.content = content
    cfg.raw['command_prefix'] = prefix
    cfg.raw['ignored_channels'] = ['honeypot']
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    with patch('cogs.moderation.CFG', cfg), \
         patch('cogs.moderation.execute_honeypot', AsyncMock(), create=True) as ban, \
         patch.object(cog, '_touch_and_mine', AsyncMock()) as touch:
        asyncio.run(cog.on_message(msg))
    ban.assert_awaited_once_with(msg)
    touch.assert_not_awaited()


def test_listener_executes_real_honeypot_rule_without_ollama():
    from cogs.moderation import ModerationCog
    msg, cfg = message(), config()
    cfg.raw['command_prefix'] = ''
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    with patch('cogs.moderation.CFG', cfg), patch.object(executor, 'CFG', cfg), \
         patch.object(executor.STORE, 'log_mod_event', AsyncMock()), \
         patch.object(executor, '_log_action', AsyncMock()), \
         patch('cogs.moderation.OLLAMA.health', AsyncMock()) as health:
        asyncio.run(cog.on_message(msg))
    msg.author.ban.assert_awaited_once()
    health.assert_not_awaited()


@pytest.mark.parametrize('bot_flag', [True, False])
def test_manual_ban_cannot_target_clawy_even_without_protected_roles(bot_flag):
    msg = message()
    msg.guild.me.id = msg.author.id
    msg.author.bot = bot_flag
    cfg = config()
    cfg.raw['protected_roles'] = []
    with patch.object(executor, 'CFG', cfg), \
         patch.object(executor.STORE, 'log_mod_event', AsyncMock()) as event, \
         patch.object(executor.STORE, 'log_bot_action', AsyncMock()), \
         patch.object(executor, '_log_action', AsyncMock()):
        result = asyncio.run(executor.execute_ban(msg.guild, msg.author, 'test', 3))
    assert result == 'refused: member is protected'
    msg.author.ban.assert_not_awaited()
    event.assert_not_awaited()


@pytest.mark.parametrize('webhook', [False, True])
def test_other_bot_or_webhook_posts_are_deleted_without_banning(webhook):
    from cogs.moderation import ModerationCog
    msg, cfg = message(), config()
    member = msg.author
    if webhook:
        msg.author = SimpleNamespace(id=8, bot=True, mention='<@8>')
    else:
        member.bot = True
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    with patch('cogs.moderation.CFG', cfg), patch.object(executor, 'CFG', cfg), \
         patch.object(executor.STORE, 'log_mod_event', AsyncMock()) as event, \
         patch.object(executor, '_log_action', AsyncMock()) as audit:
        asyncio.run(cog.on_message(msg))
    msg.delete.assert_awaited_once()
    member.ban.assert_not_awaited()
    assert event.await_args.kwargs['kind'] == 'delete'
    audit.assert_awaited_once()


def test_bot_cleanup_failure_is_not_logged_as_success():
    msg = message()
    msg.author.bot = True
    msg.delete.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='failure'), 'failure')
    with patch.object(executor, 'CFG', config()), \
         patch.object(executor.STORE, 'log_mod_event', AsyncMock()) as event, \
         patch.object(executor, '_log_action', AsyncMock()) as audit:
        result = asyncio.run(executor.execute_honeypot(msg))
    assert result == 'delete failed'
    msg.author.ban.assert_not_awaited()
    assert event.await_args.kwargs['kind'] == 'honeypot_delete_failed'
    assert 'delete FAILED' in audit.await_args.args[1]


@pytest.mark.parametrize('error', [discord.Forbidden, discord.HTTPException, discord.NotFound])
def test_delete_failure_does_not_prevent_ban_and_is_reported(error):
    msg = message()
    response = SimpleNamespace(status=404 if error is discord.NotFound else 403, reason='failure')
    msg.delete.side_effect = error(response, 'failure')
    with patch.object(executor, 'CFG', config()), \
         patch.object(executor.STORE, 'log_mod_event', AsyncMock()), \
         patch.object(executor, '_log_action', AsyncMock()) as audit:
        result = asyncio.run(executor.execute_honeypot(msg))
    assert result == 'banned'
    msg.author.ban.assert_awaited_once()
    text = audit.await_args.args[1]
    assert ('already absent' if error is discord.NotFound else 'delete FAILED') in text


def test_model_cannot_bypass_ban_guard_by_claiming_honeypot_source():
    msg = message()
    with patch.object(executor, 'CFG', config()), \
         patch.object(executor.STORE, 'log_mod_event', AsyncMock()) as event, \
         patch.object(executor.STORE, 'count_strikes', AsyncMock(return_value=0)), \
         patch.object(executor, '_log_action', AsyncMock()):
        result = asyncio.run(executor.execute({'action': 'ban', 'source': 'honeypot'}, msg))
    assert 'human review required' in result
    msg.author.ban.assert_not_awaited()
    assert event.await_args.kwargs['kind'] == 'flagged_for_ban'
