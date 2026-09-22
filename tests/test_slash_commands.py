import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cogs._common import is_admin_member, is_mod_member


def test_slash_cog_registers_supported_commands():
    async def check():
        bot = commands.Bot(command_prefix=(), intents=discord.Intents.none())
        await bot.load_extension("cogs.slash")
        try:
            names = {command.name for command in bot.tree.get_commands()}
            assert {
                "pause", "resume", "mode", "kick", "ban", "mute", "unmute",
                "purge", "purge-user", "move-last", "sleep", "wake", "roles",
                "quiet", "chat-roles", "proactive", "jumpin", "role-check", "role-grants", "role-reset",
                "react", "analyze",
                "Move message", "Delete message", "Clawy jump in",
                "Clawy react to this", "Clawy analyze this",
            } <= names
        finally:
            await bot.close()

    asyncio.run(check())


def member(*, user_id=2, administrator=False, permissions=False, roles=()):
    return SimpleNamespace(
        id=user_id,
        guild_permissions=SimpleNamespace(
            administrator=administrator,
            manage_messages=permissions,
            moderate_members=False,
        ),
        roles=[SimpleNamespace(id=role_id) for role_id in roles],
    )


def test_owner_is_admin_and_mod():
    user = member(user_id=1)
    assert is_admin_member(user, owner_id=1)
    assert is_mod_member(user, owner_id=1, mod_role_ids=set())


def test_configured_moderator_role_is_not_admin():
    user = member(roles=(42,))
    assert not is_admin_member(user, owner_id=1)
    assert is_mod_member(user, owner_id=1, mod_role_ids={42})


def test_configured_roles_disable_permission_fallback():
    user = member(permissions=True)
    assert not is_mod_member(user, owner_id=1, mod_role_ids={42})
