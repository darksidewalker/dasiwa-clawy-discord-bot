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


def _is_valid_image(data: bytes) -> bool:
    """Check magic bytes for common image formats."""
    if len(data) < 12:
        return False
    if data[:3] == b'\xff\xd8\xff':  # JPEG
        return True
    if data[:4] == b'\x89PNG':      # PNG
        return True
    if data[:6] in (b'GIF87a', b'GIF89a'):  # GIF
        return True
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':  # WebP
        return True
    if data[:2] == b'BM':           # BMP
        return True
    return False


async def _download_url(url: str, max_bytes: int) -> bytes | None:
    """Download a URL with size limit. Returns bytes or None on failure."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.debug("vision: HTTP %s for %s", resp.status, url)
                    return None

                content = bytearray()
                async for chunk in resp.content.iter_chunked(8192):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        log.warning("vision: download too large, aborting")
                        return None

                return bytes(content)
    except Exception as e:
        log.debug("vision: failed to download %s: %s", url, e)
        return None


async def fetch_attachment_image(attachment: discord.Attachment) -> str | None:
    """Download an attachment via Discord's resized proxy, return base64.

    Uses ?width=896&height=896&format=webp to keep bandwidth low (~80 KB).
    If the proxy produces an invalid image, falls back to original format.
    Returns None on any failure (wrong type, too large, network error).
    """
    ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
    if ext not in IMAGE_EXTENSIONS:
        return None

    # Try Discord CDN proxy with resize params first — keeps download tiny
    proxy_url = f"{attachment.url}?width=896&height=896&format=webp"
    data = await _download_url(proxy_url, max_image_bytes())
    if data and _is_valid_image(data):
        return base64.b64encode(data).decode("ascii")

    # Proxy produced invalid image (e.g. broken WebP conversion of GIF) — fall back to original
    log.debug("vision: proxy image invalid, falling back to original for %s", attachment.filename)
    data = await _download_url(attachment.url, max_image_bytes())
    if data and _is_valid_image(data):
        return base64.b64encode(data).decode("ascii")

    log.warning("vision: could not fetch valid image from %s", attachment.filename)
    return None


def has_image_attachments(message: discord.Message) -> bool:
    """Check if a message contains image attachments."""
    for att in message.attachments:
        ext = att.filename.rsplit(".", 1)[-1].lower() if "." in att.filename else ""
        if ext in IMAGE_EXTENSIONS:
            return True
    return False
