"""
Persona & mood management, backed by a JSON file so it's trivially editable.

The final prompt fed to the LLM is:
    <persona.base>
    <persona.moods[mood]>

Both persona and mood can be swapped at runtime via admin commands.
Changes persist to disk so they survive a restart.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

PERSONAS_PATH = Path(__file__).resolve().parent.parent / "config" / "personas.json"


class PersonaManager:
    def __init__(self, path: Path = PERSONAS_PATH) -> None:
        self.path = path
        self._lock = Lock()
        self._data: dict[str, Any] = {}
        self.reload()

    # ---------- io ----------
    def reload(self) -> None:
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)

    def _save(self) -> None:
        # Atomic write: write to temp then rename
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        tmp.replace(self.path)

    # ---------- readers ----------
    @property
    def active_key(self) -> str:
        return self._data.get("active_persona", "nyx")

    @property
    def active_mood(self) -> str:
        return self._data.get("active_mood", "neutral")

    def active_name(self) -> str:
        """Return the display name of the active persona (e.g. 'Clawy', 'Seraphael')."""
        key = self.active_key
        p = self._data.get("personas", {}).get(key, {})
        return p.get("name", key)

    def list_personas(self) -> list[str]:
        return list(self._data.get("personas", {}).keys())

    def list_moods(self, persona_key: str | None = None) -> list[str]:
        key = persona_key or self.active_key
        p = self._data.get("personas", {}).get(key, {})
        return list(p.get("moods", {}).keys())

    def describe(self, persona_key: str | None = None) -> str:
        key = persona_key or self.active_key
        p = self._data.get("personas", {}).get(key)
        if not p:
            return f"(unknown persona: {key})"
        name = p.get("name", key)
        desc = p.get("description", "")
        moods = ", ".join(self.list_moods(key))
        return f"**{name}** (`{key}`) — {desc}\nMoods: {moods}"

    def render(self, persona_key: str | None = None, mood: str | None = None) -> str:
        """Build the system prompt text for the given persona + mood."""
        key = persona_key or self.active_key
        m = mood or self.active_mood
        p = self._data.get("personas", {}).get(key)
        if not p:
            # graceful fallback
            return "You are a helpful server moderator. Be concise."
        base = p.get("base", "").strip()
        moods = p.get("moods", {})
        mood_text = moods.get(m, moods.get("neutral", "")).strip()
        return f"{base}\n{mood_text}".strip()

    # ---------- writers ----------
    def set_persona(self, key: str) -> bool:
        with self._lock:
            if key not in self._data.get("personas", {}):
                return False
            self._data["active_persona"] = key
            # reset mood to neutral (or first available) when persona changes
            moods = self.list_moods(key)
            if self._data.get("active_mood") not in moods:
                self._data["active_mood"] = "neutral" if "neutral" in moods else (moods[0] if moods else "neutral")
            self._save()
            return True

    def set_mood(self, mood: str) -> bool:
        with self._lock:
            if mood not in self.list_moods(self.active_key):
                return False
            self._data["active_mood"] = mood
            self._save()
            return True

    def add_persona(self, key: str, name: str, base: str, moods: dict[str, str] | None = None) -> None:
        with self._lock:
            self._data.setdefault("personas", {})[key] = {
                "name": name,
                "description": "",
                "base": base,
                "moods": moods or {"neutral": ""},
            }
            self._save()


PERSONAS = PersonaManager()
