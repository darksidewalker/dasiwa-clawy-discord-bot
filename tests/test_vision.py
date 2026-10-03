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


def test_image_extensions_are_detected():
    from types import SimpleNamespace
    from core.vision import has_image_attachments

    for filename in ("picture.png", "picture.JPG", "picture.webp", "animated.gif"):
        assert has_image_attachments(SimpleNamespace(attachments=[SimpleNamespace(filename=filename)]))
    for filename in ("movie.mp4", "document.pdf", "without_extension"):
        assert not has_image_attachments(SimpleNamespace(attachments=[SimpleNamespace(filename=filename)]))


def test_image_download_accepts_image_extension():
    import asyncio
    import base64
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from core.vision import fetch_attachment_image

    attachment = SimpleNamespace(filename="picture.PNG", url="https://cdn.example/picture.png")
    data = b"\x89PNG\r\n\x1a\nimage-bytes"
    with patch("core.vision._download_url", AsyncMock(return_value=data)) as download:
        result = asyncio.run(fetch_attachment_image(attachment))
    assert result == base64.b64encode(data).decode("ascii")
    download.assert_awaited_once()


def test_non_image_attachment_does_not_download():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from core.vision import fetch_attachment_image

    attachment = SimpleNamespace(filename="movie.mp4", url="https://cdn.example/movie.mp4")
    with patch("core.vision._download_url", AsyncMock()) as download:
        assert asyncio.run(fetch_attachment_image(attachment)) is None
    download.assert_not_awaited()
