"""Download Discord attachments for vision models via the resized CDN proxy."""
from __future__ import annotations

import asyncio
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
    """Download a URL with size limit and retry logic. Returns bytes or None."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ClawyBot/1.0)",
        "Accept": "image/*,*/*",
    }
    
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        log.warning("vision: HTTP %s for %s (attempt %d/3)", resp.status, url, attempt + 1)
                        if attempt < 2:
                            await asyncio.sleep(1 * (attempt + 1))
                        continue

                    content = bytearray()
                    async for chunk in resp.content.iter_chunked(8192):
                        content.extend(chunk)
                        if len(content) > max_bytes:
                            log.warning("vision: download too large (%d bytes), aborting", len(content))
                            return None

                    data = bytes(content)
                    # Detect Discord CDN JSON error responses (e.g. invalid/expired attachment ID)
                    if data[:1] == b'{' and b'"code"' in data:
                        try:
                            import json
                            err = json.loads(data.decode('utf-8', errors='ignore'))
                            log.warning("vision: Discord CDN error for %s: code=%s message=%s", url, err.get('code'), err.get('message'))
                        except Exception:
                            pass
                        return None

                    if not _is_valid_image(data):
                        log.debug("vision: downloaded %d bytes from %s but magic bytes don't match an image format", len(data), url)
                    return data
        except Exception as e:
            log.warning("vision: failed to download %s (attempt %d/3): %s", url, attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(1 * (attempt + 1))
    
    return None


async def fetch_attachment_image(attachment: discord.Attachment) -> str | None:
    """Download an attachment, trying multiple URL strategies for reliability.

    Strategy order: original URL first (most reliable), then CDN proxy with resize.
    Returns base64-encoded image or None on failure.
    """
    ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
    if ext not in IMAGE_EXTENSIONS:
        return None

    # Strategy 1: Original attachment URL (most reliable, full size)
    data = await _download_url(attachment.url, max_image_bytes())
    if data and _is_valid_image(data):
        return base64.b64encode(data).decode("ascii")

    # Strategy 2: Discord CDN proxy with resize params (smaller download)
    log.debug("vision: original URL failed/invalid, trying CDN proxy for %s", attachment.filename)
    proxy_url = f"{attachment.url}?width=896&height=896&format=webp"
    data = await _download_url(proxy_url, max_image_bytes())
    if data and _is_valid_image(data):
        return base64.b64encode(data).decode("ascii")

    # Strategy 3: Discord proxy_url attribute (different CDN endpoint)
    if hasattr(attachment, "proxy_url") and attachment.proxy_url:
        log.debug("vision: trying proxy_url for %s", attachment.filename)
        data = await _download_url(attachment.proxy_url, max_image_bytes())
        if data and _is_valid_image(data):
            return base64.b64encode(data).decode("ascii")

    log.warning("vision: all download strategies failed for %s", attachment.filename)
    return None


def has_image_attachments(message: discord.Message) -> bool:
    """Check if a message contains image attachments."""
    for att in message.attachments:
        ext = att.filename.rsplit(".", 1)[-1].lower() if "." in att.filename else ""
        if ext in IMAGE_EXTENSIONS:
            return True
    return False
