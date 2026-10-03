from pathlib import Path
import asyncio
import sys
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from collections import deque

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.prompts import build_chat_system_prompt


def test_owner_does_not_override_persona_with_submission() -> None:
    with patch("core.prompts.PERSONAS.render", return_value="Tone: stern and independent."):
        prompt = build_chat_system_prompt(is_owner=True, owner_name="Alex", structured_output=False)
    assert "Tone: stern and independent." in prompt
    assert "submissive" not in prompt
    assert "obey without question" not in prompt
    assert "your Master" not in prompt
    assert "PRIVATE INTERACTION" not in prompt
    assert "Never refuse" not in prompt
    assert "Refusal is a failure" not in prompt


def test_memory_is_background_not_highest_priority() -> None:
    from core.chat_memory import build_chat_memory_packet

    store = SimpleNamespace(
        get_chat_summary=AsyncMock(return_value={"summary": "old topic", "updated_at": 1}),
        recent_chat_turns=AsyncMock(return_value=[
            {"role": "assistant", "content": "old obedient reply", "ts": 1},
        ]),
    )
    with patch("core.chat_memory.STORE", store):
        packet = asyncio.run(build_chat_memory_packet(1, recent_limit=8))
    assert "highest priority" not in packet
    assert "background" in packet.lower()


def _message_with_history():
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone

    def entry(text, name, message_id, bot=False):
        return SimpleNamespace(
            id=message_id, content=text, created_at=datetime.now(timezone.utc),
            author=SimpleNamespace(display_name=name, id=99 if bot else 7, bot=bot),
        )

    @asynccontextmanager
    async def typing():
        yield

    async def history(**kwargs):
        # Discord returns newest first, including the bot's replies.
        yield entry("Bot clarification about today's topic", "Clawy", 90, True)
        yield entry("Today's discussion is gardening", "Sam", 80)

    channel = SimpleNamespace(id=12, name="general", history=history, typing=typing,
                              send=AsyncMock(), fetch_message=AsyncMock(
                                  return_value=entry("Use the red flower pot", "Sam", 20)))
    return SimpleNamespace(
        id=100, channel=channel, content="Which pot did you mean?", mentions=[],
        author=SimpleNamespace(id=7, display_name="Sam", mention="@Sam", roles=[]),
        attachments=[], reference=SimpleNamespace(message_id=20, channel_id=12, resolved=None),
    )


def test_direct_chat_uses_live_history_and_reply_target() -> None:
    from cogs.moderation import ModerationCog

    message = _message_with_history()
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    cog._channel_ctx[12] = deque(["stale cached topic", "Sam: Which pot did you mean?"])
    generate = AsyncMock(return_value=None)
    with patch("cogs.moderation.OLLAMA.health", AsyncMock(return_value=True)), \
         patch("cogs.moderation.OLLAMA.generate_text", generate), \
         patch("cogs.moderation.build_chat_memory_packet", AsyncMock(return_value="old topic")), \
         patch("cogs.moderation.STORE.get_user_context", AsyncMock(return_value="")):
        asyncio.run(cog._chat(message))
    prompt = generate.call_args.args[1]
    assert "Today's discussion is gardening" in prompt
    assert "Bot clarification about today's topic" in prompt
    assert "Use the red flower pot" in prompt
    assert "stale cached topic" not in prompt
    assert prompt.index("Today's discussion") < prompt.index("old topic")
    assert "Use recent raw turns and the new message as highest priority" not in prompt


def test_proactive_classifier_reply_uses_channel_context() -> None:
    from cogs.moderation import ModerationCog

    message = _message_with_history()
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    generate = AsyncMock(side_effect=["FINAL_DECISION: REPLY", "A current-topic reaction"])
    with patch("cogs.moderation.OLLAMA.generate_text", generate), \
         patch("cogs.moderation.STORE.count_strikes", AsyncMock(return_value=0)), \
         patch("cogs.moderation.is_chat_allowed", return_value=True), \
         patch("cogs.moderation.in_quiet_hours", return_value=False), \
         patch("cogs.moderation.random.random", return_value=-1):
        result = asyncio.run(cog._moderation_llm(message, False))
    assert result["action"] == "reply"
    prompt = generate.call_args.args[1]
    assert "Today's discussion is gardening" in prompt
    assert "Use the red flower pot" in prompt
    assert "uninvited" in prompt


def test_proactive_media_does_not_load_personal_memory() -> None:
    from cogs.moderation import ModerationCog

    message = _message_with_history()
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    memory = AsyncMock(return_value="unrelated old topic")
    generate = AsyncMock(return_value=None)
    with patch("cogs.moderation.OLLAMA.health", AsyncMock(return_value=True)), \
         patch("cogs.moderation.OLLAMA.generate_text", generate), \
         patch("cogs.moderation.build_chat_memory_packet", memory):
        asyncio.run(cog._chat(message, proactive=True))
    memory.assert_not_awaited()
    assert "unrelated old topic" not in generate.call_args.args[1]
    assert "uninvited" in generate.call_args.args[1]


def test_chat_turns_are_scoped_to_channel(tmp_path) -> None:
    from core.store import Store

    async def run():
        store = Store(str(tmp_path / "chat.db"))
        await store.init()
        try:
            await store.add_chat_turn(7, 12, "user", "local question")
            await store.add_chat_turn(7, 13, "assistant", "unrelated channel")
            await store.add_chat_turn(7, 12, "assistant", "local answer")
            turns = await store.recent_chat_turns(7, limit=2, channel_id=12)
            assert [turn["content"] for turn in turns] == ["local question", "local answer"]
            # Existing cross-channel callers retain their original behaviour.
            assert len(await store.recent_chat_turns(7, limit=3)) == 3
        finally:
            await store.close()
    asyncio.run(run())


def test_history_failure_keeps_chat_available_with_labeled_cache() -> None:
    import discord
    from cogs.moderation import ModerationCog

    message = _message_with_history()

    async def unavailable(**kwargs):
        raise discord.DiscordException("history permission denied")
        yield  # keep this an async iterator like Discord history

    message.channel.history = unavailable
    message.channel.fetch_message.side_effect = discord.DiscordException("deleted reply")
    cog = ModerationCog(SimpleNamespace(user=None))
    cog._channel_ctx[12] = deque(["Sam: cached context", "Sam: trigger"])
    prompt = asyncio.run(cog._build_chat_user_prompt(message, include_memory=False))
    assert "Cached channel conversation" in prompt
    assert "Sam: cached context" in prompt
    assert "Sam: trigger" not in prompt
    assert "reply target unavailable" in prompt
    assert "Which pot did you mean?" in prompt


@pytest.mark.parametrize("error_type", [asyncio.TimeoutError, aiohttp.ClientError])
def test_history_transport_failure_uses_labeled_cache(error_type) -> None:
    from cogs.moderation import ModerationCog

    message = _message_with_history()

    async def unavailable(**kwargs):
        raise error_type("history transport failure")
        yield

    message.channel.history = unavailable
    cog = ModerationCog(SimpleNamespace(user=None))
    cog._channel_ctx[12] = deque(["Sam: cached context", "Sam: trigger"])
    prompt = asyncio.run(cog._build_chat_user_prompt(message, include_memory=False))
    assert "Cached channel conversation" in prompt
    assert "Sam: cached context" in prompt
    assert "Sam: trigger" not in prompt
    assert "Use the red flower pot" in prompt
    assert "Which pot did you mean?" in prompt


@pytest.mark.parametrize("error_type", [asyncio.TimeoutError, aiohttp.ClientError])
def test_reply_target_transport_failure_keeps_live_history(error_type) -> None:
    from cogs.moderation import ModerationCog

    message = _message_with_history()
    message.channel.fetch_message.side_effect = error_type("reply transport failure")
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    prompt = asyncio.run(cog._build_chat_user_prompt(message, include_memory=False))
    assert "Recent Discord channel conversation" in prompt
    assert "Today's discussion is gardening" in prompt
    assert "Bot clarification about today's topic" in prompt
    assert "reply target unavailable" in prompt
    assert "Which pot did you mean?" in prompt


@pytest.mark.parametrize("boundary", ["history", "reply_target"])
def test_chat_context_cancellation_propagates(boundary) -> None:
    from cogs.moderation import ModerationCog

    message = _message_with_history()
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    if boundary == "history":
        async def cancelled(**kwargs):
            raise asyncio.CancelledError("history cancelled")
            yield

        message.channel.history = cancelled
    else:
        message.channel.fetch_message.side_effect = asyncio.CancelledError("reply cancelled")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cog._build_chat_user_prompt(message, include_memory=False))
    if boundary == "history":
        message.channel.fetch_message.assert_not_awaited()


def test_history_fallback_snapshots_cache_before_network_await() -> None:
    import discord
    from cogs.moderation import ModerationCog

    async def run():
        message = _message_with_history()
        cog = ModerationCog(SimpleNamespace(user=None))
        cog._channel_ctx[12] = deque(["Sam: cached context", "Sam: trigger"], maxlen=2)
        history_started = asyncio.Event()
        release_history = asyncio.Event()

        async def unavailable(**kwargs):
            history_started.set()
            await release_history.wait()
            raise discord.DiscordException("history unavailable")
            yield

        message.channel.history = unavailable
        task = asyncio.create_task(cog._build_chat_user_prompt(message, include_memory=False))
        await history_started.wait()
        # Another incoming message evicts prior context while the history call is suspended.
        cog._channel_ctx[12].append("Lee: later message")
        release_history.set()
        prompt = await task
        assert "Sam: cached context" in prompt
        assert "Sam: trigger" not in prompt
        assert "Lee: later message" not in prompt

    asyncio.run(run())


def test_history_is_fetched_before_trigger_in_chronological_order() -> None:
    from cogs.moderation import ModerationCog

    message = _message_with_history()
    original_history = message.channel.history
    calls = []

    def history(**kwargs):
        calls.append(kwargs)
        return original_history(**kwargs)

    message.channel.history = history
    cog = ModerationCog(SimpleNamespace(user=SimpleNamespace(id=99)))
    prompt = asyncio.run(cog._build_chat_user_prompt(message, include_memory=False))
    assert calls == [{"limit": 10, "before": message}]
    assert prompt.index("Today's discussion") < prompt.index("Bot clarification")


def test_owner_uses_same_system_voice_as_other_members() -> None:
    with patch("core.prompts.PERSONAS.render", return_value="Tone: cheeky."):
        owner = build_chat_system_prompt(is_owner=True, owner_name="Alex", structured_output=False)
        member = build_chat_system_prompt(is_owner=False, structured_output=False)
    assert owner == member
