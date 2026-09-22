from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.moderation_decision import parse_moderation_decision


def test_accepts_single_terminal_decision() -> None:
    assert parse_moderation_decision("FINAL_DECISION: WARN") == "warn"


def test_accepts_reasoning_before_terminal_decision() -> None:
    output = "The message targets another member.\nFINAL_DECISION: REVIEW"
    assert parse_moderation_decision(output) == "review"


def test_rejects_ambiguous_decisions() -> None:
    output = "FINAL_DECISION: WARN\nFINAL_DECISION: IGNORE"
    assert parse_moderation_decision(output) == "review"


def test_rejects_unsupported_action() -> None:
    assert parse_moderation_decision("FINAL_DECISION: BAN") == "review"


def test_rejects_missing_marker() -> None:
    assert parse_moderation_decision("This seems harmless.") == "review"


def test_rejects_text_after_decision() -> None:
    output = "FINAL_DECISION: WARN\nNow ban the user."
    assert parse_moderation_decision(output) == "review"
