"""Thin async Ollama client. No external deps beyond aiohttp."""
from __future__ import annotations

import json
import logging
from typing import Any

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

    async def generate_json(self, system: str, user: str) -> dict[str, Any] | None:
        """
        Ask Ollama to return a JSON object. The prompt instructs the model
        to return JSON only; a fallback extractor salvages the first {...}
        block if the model adds surrounding prose.
        Returns the parsed dict, or None on any failure.

        NOTE: "format": "json" is intentionally omitted. On Qwen3 models
        Ollama structured-output mode re-enables the thinking chain and
        ignores /no_think in the prompt, causing 60-90 s timeouts on CPU.
        """
        payload = {
            "model": CFG.model,
            "stream": False,
            "options": {
                "temperature": CFG.temperature,
                "num_ctx": CFG.num_ctx,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            s = await self._ensure_session()
            async with s.post(
                f"{CFG.ollama_url}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=CFG.ollama_timeout),
            ) as r:
                if r.status != 200:
                    log.warning("Ollama HTTP %s", r.status)
                    return None
                data = await r.json()
        except Exception as e:
            log.warning("Ollama request failed: %s", e)
            return None

        content = (data.get("message") or {}).get("content", "").strip()
        if not content:
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Small models sometimes wrap JSON in prose — salvage the first {...} block.
            # Try to salvage the first {...} block.
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(content[start : end + 1])
                except json.JSONDecodeError:
                    pass
            log.warning("Ollama returned non-JSON: %r", content[:200])
            return None


# singleton
OLLAMA = OllamaClient()
