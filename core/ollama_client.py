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
        Ask Ollama to return a JSON object. Uses `format: json` which forces
        valid JSON output on models that support it (llama3.*, qwen2.5, phi3, mistral, ...).
        Returns the parsed dict, or None on any failure.

        gemma4 bug: think=false + format="json" causes format to be silently
        ignored (Ollama issue #15260). Workaround: only send `think` when it
        is True — omitting it entirely lets format work on gemma4.
        gemma4:e2b/e4b don't think by default so nothing is lost.
        """
        payload: dict[str, Any] = {
            "model": CFG.model,
            "stream": False,
            "options": {
                "temperature": CFG.temperature,
                "num_ctx": CFG.num_ctx,
                "num_predict": CFG.num_predict,
                "num_thread": getattr(CFG, "num_thread", 4),
                "f16_kv": getattr(CFG, "f16_kv", False),
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # format:json causes HTTP 500 on some models (e.g. gemma4:e4b).
        # Disable via use_json_format: false in config. The JSON extractor
        # below handles plain-text responses with embedded JSON.
        if CFG.use_json_format:
            payload["format"] = "json"
        # Only inject `think` when explicitly enabled — sending think=false
        # breaks format/JSON mode on gemma4 and qwen3.5 (Ollama bug #15260).
        if CFG.think:
            payload["think"] = True
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

        # Strip thinking tokens that gemma4 may emit even with think omitted
        # Format: <|channel>thought\n...<channel|>  or  <think>...</think>
        import re as _re
        content = _re.sub(
            r"<\|channel>thought.*?<channel\|>",
            "", content, flags=_re.DOTALL
        ).strip()
        content = _re.sub(
            r"<think>.*?</think>",
            "", content, flags=_re.DOTALL
        ).strip()

        # Strip markdown fences — gemma4 wraps JSON in ```json ... ``` blocks
        # even when format="json" is set (Ollama issue #15595)
        if content.startswith("```"):
            content = _re.sub(r"^```[a-zA-Z]*\n?", "", content)
            content = _re.sub(r"```$", "", content).strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Last resort: grab the first {...} block
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
