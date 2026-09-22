"""Tests for vision attachment handling."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import CFG
from core.vision import vision_enabled, max_image_bytes


def test_vision_disabled_by_default() -> None:
    original = CFG.raw.get("vision")
    try:
        if "vision" in CFG.raw:
            del CFG.raw["vision"]
        assert not vision_enabled()
    finally:
        if original is not None:
            CFG.raw["vision"] = original


def test_vision_config_values() -> None:
    original = CFG.raw.get("vision")
    try:
        CFG.raw["vision"] = {"enabled": True, "max_bytes": 1024}
        assert vision_enabled()
        assert max_image_bytes() == 1024
    finally:
        if original is not None:
            CFG.raw["vision"] = original
        elif "vision" in CFG.raw:
            del CFG.raw["vision"]
