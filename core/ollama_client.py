"""Thin async Ollama client. No external deps beyond aiohttp."""
from __future__ import annotations

import logging

import aiohttp

from .config import CFG

log = logging.getLogger(__name__)


class OllamaClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def health(self) -> bool:
        """Return True if Ollama answers /api/tags."""
        try:
            s = await self._ensure_session()
            async with s.get(f"{CFG.ollama_url}/api/tags", timeout=5) as r:
                return r.status == 200
        except Exception as e:
            log.warning("Ollama health check failed: %s", e)
            return False

    @staticmethod
    def _build_payload(system: str, user: str, *, think: bool) -> dict[str, object]:
        """Build a plain-text request with thinking explicitly controlled."""
        return {
            "model": CFG.model,
            "stream": False,
            "think": think,
            "options": {
                "temperature": CFG.temperature,
                "num_ctx": CFG.num_ctx,
                "num_predict": CFG.num_predict,
                "num_thread": CFG.num_thread,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

    async def generate_text(self, system: str, user: str) -> str | None:
        """Generate an ordinary text response without requiring structured output."""
        payload = self._build_payload(system, user, think=CFG.think)
        try:
            s = await self._ensure_session()
            async with s.post(
                f"{CFG.ollama_url}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=CFG.ollama_timeout),
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning("Ollama HTTP %s: %s", r.status, body[:300])
                    return None
                data = await r.json()
        except Exception as e:
            log.warning("Ollama text request failed: %s", e)
            return None

        content = (data.get("message") or {}).get("content", "").strip()
        if not content:
            log.warning("Ollama returned an empty text response")
            return None
        log.info("Ollama raw text response: %r", content[:500])
        return self._clean_text(content)

    @staticmethod
    def _clean_text(content: str) -> str:
        """Remove common reasoning wrappers while preserving ordinary prose."""
        import re as _re

        content = _re.sub(
            r"<\|channel>thought.*?<channel\|>",
            "", content, flags=_re.DOTALL,
        ).strip()
        content = _re.sub(
            r"<think>.*?</think>",
            "", content, flags=_re.DOTALL,
        ).strip()
        if content.startswith("```") and content.endswith("```"):
            content = _re.sub(r"^```[a-zA-Z]*\n?", "", content)
            content = _re.sub(r"```$", "", content).strip()
        return content



# singleton
OLLAMA = OllamaClient()
