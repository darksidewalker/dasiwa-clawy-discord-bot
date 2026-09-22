from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.ollama_client import OllamaClient


def test_plain_payload_explicitly_disables_thinking() -> None:
    payload = OllamaClient._build_payload("system", "user", think=False)
    assert payload["think"] is False
    assert "f16_kv" not in payload["options"]


def test_clean_text_preserves_plain_response() -> None:
    assert OllamaClient._clean_text("Hello there.") == "Hello there."


def test_clean_text_removes_think_block() -> None:
    content = "<think>private reasoning</think>\nHello there."
    assert OllamaClient._clean_text(content) == "Hello there."


def test_clean_text_removes_gemma_thought_channel() -> None:
    content = "<|channel>thought\nprivate reasoning<channel|>\nHello there."
    assert OllamaClient._clean_text(content) == "Hello there."


def test_clean_text_unwraps_markdown_fence() -> None:
    assert OllamaClient._clean_text("```text\nHello there.\n```") == "Hello there."
