from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cogs.moderation import ModerationCog
from core.config import Config


@asynccontextmanager
async def _typing():
    yield


def _message(content="An ordinary question???"):
    return SimpleNamespace(
        id=100, content=content, guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=7, bot=False, display_name="Sam", roles=[],
                               created_at=datetime.now(timezone.utc)),
        channel=SimpleNamespace(id=12, name="general", typing=_typing),
        mentions=[], attachments=[], embeds=[],
    )


@pytest.mark.parametrize("chance,roll,recent,mode", [
    (0.0, 0.0, False, "chat_and_moderate"),
    (0.5, 0.9, False, "chat_and_moderate"),
    (1.0, 0.0, True, "chat_and_moderate"),
    (1.0, 0.0, False, "moderate_only"),
])
def test_noteworthy_classifier_cannot_bypass_proactive_policy(chance, roll, recent, mode):
    config = Config(raw={"mode": mode, "moderation": {
        "proactive_reply_chance": chance, "proactive_reply_cooldown_seconds": 300,
    }})
    message = _message()
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    if recent:
        cog._last_proactive[12] = 999.0
    generate = AsyncMock(side_effect=["FINAL_DECISION: REPLY", "Uninvited reply"])
    with patch("cogs.moderation.CFG", config), \
         patch("cogs.moderation.is_chat_allowed", return_value=True), \
         patch("cogs.moderation.in_quiet_hours", return_value=False), \
         patch("cogs.moderation.time.time", return_value=1000.0), \
         patch("cogs.moderation.random.random", return_value=roll), \
         patch("cogs.moderation.STORE.count_strikes", AsyncMock(return_value=0)), \
         patch("cogs.moderation.OLLAMA.generate_text", generate), \
         patch.object(cog, "_build_chat_user_prompt", AsyncMock(return_value="Live context")):
        result = asyncio.run(cog._moderation_llm(message, False))
    assert result is None or result["action"] == "ignore"
    assert generate.await_count <= 1


@pytest.mark.parametrize("chance,roll,recent,expected", [
    (0.0, 0.0, False, False),
    (0.5, 0.9, False, False),
    (1.0, 0.0, True, False),
    (1.0, 0.0, False, True),
])
def test_image_only_messages_respect_chance_and_cooldown(chance, roll, recent, expected):
    config = Config(raw={"mode": "chat_only", "triggers": {"enabled": False},
                         "moderation": {"proactive_reply_chance": chance,
                                        "proactive_reply_cooldown_seconds": 300}})
    message = _message("")
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(
        id=99, display_name="Clawy", name="Clawy")))
    if recent:
        cog._last_proactive[12] = 999.0
    chat = AsyncMock()
    with patch("cogs.moderation.CFG", config), \
         patch("cogs.moderation.is_chat_allowed", return_value=True), \
         patch("cogs.moderation.in_quiet_hours", return_value=False), \
         patch("cogs.moderation.vision_enabled", return_value=True), \
         patch("cogs.moderation.has_image_attachments", return_value=True), \
         patch("cogs.moderation.time.time", return_value=1000.0), \
         patch("cogs.moderation.random.random", return_value=roll), \
         patch.object(cog, "_touch_and_mine", AsyncMock()), \
         patch.object(cog, "_chat", chat):
        asyncio.run(cog.on_message(message))
    assert chat.await_count == int(expected)
    if expected:
        chat.assert_awaited_once_with(message, proactive=True)
        assert cog._last_proactive[12] == 1000.0


@pytest.mark.parametrize("allowed,quiet", [(True, False), (False, False), (True, True)])
def test_zero_chance_does_not_disable_noteworthy_moderation(allowed, quiet):
    config = Config(raw={"mode": "chat_and_moderate", "moderation": {
        "proactive_reply_chance": 0.0,
    }})
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    generate = AsyncMock(side_effect=["FINAL_DECISION: WARN", "Please follow the rules."])
    with patch("cogs.moderation.CFG", config), \
         patch("cogs.moderation.is_chat_allowed", return_value=allowed), \
         patch("cogs.moderation.in_quiet_hours", return_value=quiet), \
         patch("cogs.moderation.STORE.count_strikes", AsyncMock(return_value=0)), \
         patch("cogs.moderation.OLLAMA.generate_text", generate):
        result = asyncio.run(cog._moderation_llm(_message(), False))
    assert result is not None
    assert result["action"] == "warn"
    assert result["message"] == "Please follow the rules."


def test_successful_proactive_roll_can_reply():
    config = Config(raw={"mode": "chat_and_moderate", "moderation": {
        "proactive_reply_chance": 0.5,
    }})
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    generate = AsyncMock(side_effect=["FINAL_DECISION: REPLY", "A relevant reaction"])
    with patch("cogs.moderation.CFG", config), \
         patch("cogs.moderation.is_chat_allowed", return_value=True), \
         patch("cogs.moderation.in_quiet_hours", return_value=False), \
         patch("cogs.moderation.random.random", return_value=0.1), \
         patch("cogs.moderation.STORE.count_strikes", AsyncMock(return_value=0)), \
         patch("cogs.moderation.OLLAMA.generate_text", generate), \
         patch.object(cog, "_build_chat_user_prompt", AsyncMock(return_value="Live context")):
        result = asyncio.run(cog._moderation_llm(_message("Normal conversation"), False))
    assert result is not None
    assert result["action"] == "reply"
    assert result["message"] == "A relevant reaction"


@pytest.mark.parametrize("content,mentioned", [
    ("Hello there", True), ("Clawy, hello", False),
])
def test_zero_chance_keeps_direct_chat(content, mentioned):
    config = Config(raw={"mode": "chat_and_moderate", "triggers": {"enabled": False},
                         "moderation": {"proactive_reply_chance": 0.0}})
    message = _message(content)
    bot_user = SimpleNamespace(id=99, display_name="Clawy", name="Clawy")
    if mentioned:
        message.mentions = [bot_user]
    cog = ModerationCog(SimpleNamespace(user=bot_user))
    chat = AsyncMock()
    with patch("cogs.moderation.CFG", config), \
         patch("cogs.moderation.is_chat_allowed", return_value=True), \
         patch("cogs.moderation.in_quiet_hours", return_value=False), \
         patch("cogs.moderation.MENTION_RL.record"), \
         patch("cogs.moderation.MENTION_RL.check", return_value=None), \
         patch("cogs.moderation.prefilter", AsyncMock(return_value=("llm", {}))), \
         patch.object(cog, "_touch_and_mine", AsyncMock()), \
         patch.object(cog, "_chat", chat):
        asyncio.run(cog.on_message(message))
    chat.assert_awaited_once_with(message)
