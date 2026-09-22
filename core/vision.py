"""Download Discord attachments for vision models via the resized CDN proxy."""
from __future__ import annotations

import base64
import logging

import aiohttp
import discord

from .config import CFG

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def vision_enabled() -> bool:
    return bool(CFG.raw.get("vision", {}).get("enabled", False))


def max_image_bytes() -> int:
    """Max bytes to download before aborting. Default 5 MB."""
    return int(CFG.raw.get("vision", {}).get("max_bytes", 5 * 1024 * 1024))


async def fetch_attachment_image(attachment: discord.Attachment) -> str | None:
    """Download an attachment via Discord's resized proxy, return base64.

    Uses ?width=896&height=896&format=webp to keep bandwidth low (~80 KB).
    Returns None on any failure (wrong type, too large, network error).
    """
    ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
    if ext not in IMAGE_EXTENSIONS:
        return None

    # Discord CDN proxy with resize params — keeps download tiny
    url = f"{attachment.url}?width=896&height=896&format=webp"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.debug("vision: HTTP %s for attachment", resp.status)
                    return None

                content = bytearray()
                async for chunk in resp.content.iter_chunked(8192):
                    content.extend(chunk)
                    if len(content) > max_image_bytes():
                        log.warning("vision: attachment too large, aborting")
                        return None

        return base64.b64encode(bytes(content)).decode("ascii")
    except Exception as e:
        log.debug("vision: failed to download attachment: %s", e)
        return None


def has_image_attachments(message: discord.Message) -> bool:
    """Check if a message contains image attachments."""
    for att in message.attachments:
        ext = att.filename.rsplit(".", 1)[-1].lower() if "." in att.filename else ""
        if ext in IMAGE_EXTENSIONS:
            return True
    return False
