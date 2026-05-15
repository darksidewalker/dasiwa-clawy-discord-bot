"""Load YAML config and expose mutable runtime state."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"

BotMode = Literal["moderate_only", "chat_and_moderate", "chat_only"]
VALID_MODES: tuple[BotMode, ...] = ("moderate_only", "chat_and_moderate", "chat_only")


@dataclass
class RuntimeState:
    """Things that can change at runtime without a restart.

    Persona/mood are NOT here — they live in PersonaManager (core/persona.py),
    which persists to config/personas.json.
    """
    paused: bool = False
    model_override: str | None = None
    mode_override: BotMode | None = None
    # Sleep mode
    sleeping: bool = False
    wake_at: float = 0.0   # unix timestamp; 0.0 = no auto-wake scheduled
    # Ollama thinking toggle. None = use YAML default (ollama.think).
    think_override: bool | None = None
    # Chat gating — session overrides
    quiet_hours_enabled_override: bool | None = None
    quiet_hours_start_override: str | None = None   # "HH:MM"
    quiet_hours_end_override: str | None = None     # "HH:MM"
    quiet_hours_timezone_override: str | None = None
    chat_allowed_roles_override: list[str] | None = None
    # Proactive reply chance override (None = use YAML)
    proactive_chance_override: float | None = None


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    state: RuntimeState = field(default_factory=RuntimeState)

    @classmethod
    def load(cls) -> "Config":
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(raw=raw)

    def reload_yaml(self) -> None:
        """Re-read config.yaml from disk. Keeps runtime state untouched."""
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            self.raw = yaml.safe_load(f) or {}

    # ---- env ----
    @property
    def discord_token(self) -> str:
        tok = os.environ.get("DISCORD_TOKEN", "").strip()
        if not tok or tok.startswith("paste-"):
            raise RuntimeError("DISCORD_TOKEN is missing. Set it in .env")
        return tok

    @property
    def ollama_url(self) -> str:
        return os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")

    # ---- discord basics ----
    @property
    def guild_id(self) -> int:
        return int(self.raw.get("guild_id", 0))

    @property
    def owner_id(self) -> int:
        return int(self.raw.get("owner_id", 0))

    @property
    def log_channel_id(self) -> int:
        return int(self.raw.get("log_channel_id", 0))

    @property
    def command_prefix(self) -> str:
        return self.raw.get("command_prefix", "!")

    @property
    def protected_roles(self) -> list[str]:
        return list(self.raw.get("protected_roles", []))

    @property
    def ignored_channels(self) -> list[str]:
        return list(self.raw.get("ignored_channels", []))

    @property
    def nsfw_channels(self) -> list[str]:
        """Channel names marked as NSFW/adult. Moderation is relaxed for adult content there."""
        return list(self.raw.get("nsfw_channels", []))

    @property
    def dynamic_mood(self) -> bool:
        """If True, the LLM can autonomously switch its mood based on conversation context."""
        return bool(self.raw.get("dynamic_mood", False))

    # ---- mode ----
    @property
    def mode(self) -> BotMode:
        if self.state.mode_override:
            return self.state.mode_override
        m = self.raw.get("mode", "chat_and_moderate")
        if m not in VALID_MODES:
            m = "chat_and_moderate"
        return m  # type: ignore[return-value]

    @property
    def chat_enabled(self) -> bool:
        return self.mode in ("chat_and_moderate", "chat_only")

    @property
    def moderation_enabled(self) -> bool:
        return self.mode in ("chat_and_moderate", "moderate_only")

    # ---- storage ----
    @property
    def db_path(self) -> str:
        return str(self.raw.get("database", {}).get("path", "data/bot.db"))

    @property
    def chat_keep_last_turns(self) -> int:
        return int(self.raw.get("database", {}).get("chat_keep_last_turns", 50))

    @property
    def chat_context_turns(self) -> int:
        return int(self.raw.get("database", {}).get("chat_context_turns", 8))

    # ---- ollama ----
    @property
    def model(self) -> str:
        return self.state.model_override or self.raw.get("ollama", {}).get("model", "qwen2.5:3b")

    @property
    def temperature(self) -> float:
        return float(self.raw.get("ollama", {}).get("temperature", 0.3))

    @property
    def num_ctx(self) -> int:
        return int(self.raw.get("ollama", {}).get("num_ctx", 4096))

    @property
    def num_predict(self) -> int:
        """Max tokens to generate. -1 = unlimited (Ollama default is 128)."""
        return int(self.raw.get("ollama", {}).get("num_predict", 256))

    @property
    def use_json_format(self) -> bool:
        """Whether to send format=json to Ollama. Disable for models that 500 on it (e.g. gemma4)."""
        return bool(self.raw.get("ollama", {}).get("use_json_format", True))

    @property
    def ollama_timeout(self) -> int:
        return int(self.raw.get("ollama", {}).get("timeout_seconds", 30))

    @property
    def num_thread(self) -> int:
        """Threads Ollama uses for inference. Default 4 if not set."""
        return int(self.raw.get("ollama", {}).get("num_thread", 4))

    @property
    def f16_kv(self) -> bool:
        """Whether to use fp16 for the KV cache. Default False (saves RAM on CPU)."""
        return bool(self.raw.get("ollama", {}).get("f16_kv", False))

    @property
    def think(self) -> bool:
        """
        Whether Ollama should run the model's reasoning trace before answering.
        Resolution: runtime !think override > YAML ollama.think > False.
        """
        if self.state.think_override is not None:
            return self.state.think_override
        return bool(self.raw.get("ollama", {}).get("think", False))

    # ---- chat gating ----

    @property
    def chat_allowed_roles(self) -> list[str]:
        """Role NAMES that are allowed to chat with Clawy.
        Empty list = everyone can chat (current default behavior).
        Session override via !chatroles takes precedence over YAML.
        """
        if self.state.chat_allowed_roles_override is not None:
            return list(self.state.chat_allowed_roles_override)
        return list(self.raw.get("chat", {}).get("allowed_roles", []))

    @property
    def quiet_hours_enabled(self) -> bool:
        if self.state.quiet_hours_enabled_override is not None:
            return self.state.quiet_hours_enabled_override
        return bool(self.raw.get("chat", {}).get("quiet_hours", {}).get("enabled", False))

    @property
    def quiet_hours_start(self) -> str:
        """24h HH:MM string (e.g. '23:00'). Session override > YAML > '23:00'."""
        if self.state.quiet_hours_start_override is not None:
            return self.state.quiet_hours_start_override
        return str(self.raw.get("chat", {}).get("quiet_hours", {}).get("start", "23:00"))

    @property
    def quiet_hours_end(self) -> str:
        if self.state.quiet_hours_end_override is not None:
            return self.state.quiet_hours_end_override
        return str(self.raw.get("chat", {}).get("quiet_hours", {}).get("end", "07:00"))

    @property
    def quiet_hours_timezone(self) -> str:
        """IANA timezone name (e.g. 'Europe/Berlin'). Default: UTC."""
        if self.state.quiet_hours_timezone_override is not None:
            return self.state.quiet_hours_timezone_override
        return str(self.raw.get("chat", {}).get("quiet_hours", {}).get("timezone", "UTC"))

    @property
    def proactive_reply_chance(self) -> float:
        """0.0 = off, 1.0 = reply to every message."""
        if self.state.proactive_chance_override is not None:
            return self.state.proactive_chance_override
        return float(self.raw.get("moderation", {}).get("proactive_reply_chance", 0.0))

    # ---- moderation ----
    @property
    def mod(self) -> dict[str, Any]:
        return self.raw.get("moderation", {})

    @property
    def allowed_actions(self) -> set[str]:
        return set(self.raw.get("allowed_actions", []))

    @property
    def max_autonomous_timeout_seconds(self) -> int:
        return int(self.raw.get("max_autonomous_timeout_seconds", 600))

    @property
    def respond_to_persona_name(self) -> bool:
        """
        If True, the bot replies when addressed by the ACTIVE PERSONA's name
        (e.g. 'Seraphael hello' triggers a response when seraphael is active).
        If False (default), only the bot's Discord display name triggers replies,
        avoiding confusion when personas change.
        """
        return bool(self.raw.get("respond_to_persona_name", False))

    # ---- move ----
    @property
    def move_max_batch(self) -> int:
        return int(self.raw.get("move", {}).get("max_batch", 25))

    @property
    def move_post_notice(self) -> bool:
        return bool(self.raw.get("move", {}).get("post_notice", True))

    # ---- notify_user (DM + channel notice on delete/move/purge) ----
    @property
    def notify_user_enabled(self) -> bool:
        """Master switch for user notifications on delete/move/purge.
        When False, both DM and channel notice are suppressed everywhere."""
        return bool(self.raw.get("notify_user", {}).get("enabled", True))

    @property
    def notify_user_dm(self) -> bool:
        """Whether to DM the affected user. Best-effort — silent if DMs disabled.
        Has no effect when notify_user.enabled is False."""
        return bool(self.raw.get("notify_user", {}).get("dm", True))

    @property
    def notify_user_channel_notice(self) -> bool:
        """Whether to post the auto-deleting channel notice.
        Has no effect when notify_user.enabled is False."""
        return bool(self.raw.get("notify_user", {}).get("channel_notice", True))

    @property
    def notify_user_notice_seconds(self) -> int:
        """How long the channel notice stays visible before auto-deleting.
        Clamped to [3, 300]."""
        v = int(self.raw.get("notify_user", {}).get("notice_seconds", 20))
        return max(3, min(v, 300))

    # ---- expressions (reactions / stickers / media pool) ----
    @property
    def expressions_enabled(self) -> bool:
        """Master switch for emoji reactions, stickers, and media attachments.
        When False, none of the expressions.* features fire and the prompt
        injection that advertises them to the LLM is skipped."""
        return bool(self.raw.get("expressions", {}).get("enabled", True))

    @property
    def expressions_allow_reactions(self) -> bool:
        return bool(self.raw.get("expressions", {}).get("allow_reactions", True))

    @property
    def expressions_allow_stickers(self) -> bool:
        return bool(self.raw.get("expressions", {}).get("allow_stickers", True))

    @property
    def expressions_allow_attachments(self) -> bool:
        return bool(self.raw.get("expressions", {}).get("allow_attachments", True))

    @property
    def expressions_prompt_limit(self) -> int:
        """How many of each category (emoji / stickers / media) to advertise
        in the system prompt per turn. Higher = more variety but more tokens
        spent. Random sampling so the LLM sees different items over time.
        Clamped to [3, 100]."""
        v = int(self.raw.get("expressions", {}).get("prompt_limit", 30))
        return max(3, min(v, 100))

    @property
    def expressions_max_reactions(self) -> int:
        """Hard cap on how many reactions Clawy may add per message.
        Discord's hard limit is 20, but anything above ~3 is visual spam.
        Clamped to [1, 20]."""
        v = int(self.raw.get("expressions", {}).get("max_reactions_per_message", 3))
        return max(1, min(v, 20))

    # ---- triggers (deterministic media-on-keyword) ----
    @property
    def triggers_enabled(self) -> bool:
        """Master switch for keyword/regex triggers that auto-post media.
        When False, the triggers manager is loaded but never consulted by
        the moderation pipeline."""
        return bool(self.raw.get("triggers", {}).get("enabled", True))

    @property
    def triggers_max_per_message(self) -> int:
        """Maximum number of triggers that may fire on a single user message.
        Default and recommended value is 1 — more than one media post in
        response to a single message looks like spam. Clamped to [1, 5]."""
        v = int(self.raw.get("triggers", {}).get("max_per_message", 1))
        return max(1, min(v, 5))


# Module-level singleton
CFG = Config.load()
