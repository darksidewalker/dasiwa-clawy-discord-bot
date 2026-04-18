"""
In-memory spam rate tracking only.

Strikes are now persisted in SQLite (see core/store.py::count_strikes).
Spam detection is a 10-second sliding window — storing that in SQLite would be
pure overhead. It's fine to lose it on restart.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from .config import CFG


class SpamTracker:
    """Sliding-window message counter per user. Check is non-destructive."""

    def __init__(self) -> None:
        self._msgs: dict[int, deque[float]] = defaultdict(deque)

    def record(self, user_id: int) -> int:
        window = CFG.mod.get("spam_window_seconds", 10)
        now = time.time()
        dq = self._msgs[user_id]
        dq.append(now)
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq)

    def is_spamming(self, user_id: int) -> bool:
        """Check current rate WITHOUT recording. Call record() on the actual message first."""
        threshold = CFG.mod.get("spam_threshold", 6)
        window = CFG.mod.get("spam_window_seconds", 10)
        now = time.time()
        dq = self._msgs[user_id]
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq) >= threshold


SPAM = SpamTracker()


class MentionRateLimiter:
    """
    Tracks how often a user @mentions the bot within a sliding window.

    Escalation logic:
      - First breach  → warn  (in-channel reply, no punishment)
      - Second breach → timeout (duration from config)

    A "breach" is defined as: more than `max_mentions` bot-mentions
    within `window_seconds`. The strike resets after `reset_after_seconds`
    of silence (i.e. no further breach).
    """

    # Defaults — overridden by config values if present
    _DEFAULT_MAX      = 4    # max @mentions allowed in the window
    _DEFAULT_WINDOW   = 30   # seconds the window spans
    _DEFAULT_RESET    = 120  # seconds of quiet before the strike resets
    _DEFAULT_TIMEOUT  = 300  # mute duration on second breach (seconds)

    def __init__(self) -> None:
        # user_id -> deque of timestamps of bot-mention events
        self._mentions: dict[int, deque[float]] = defaultdict(deque)
        # user_id -> timestamp of last issued warning (0 = none)
        self._warned_at: dict[int, float] = defaultdict(float)

    def _cfg(self) -> tuple[int, int, int, int]:
        m = CFG.mod
        return (
            int(m.get("mention_max",            self._DEFAULT_MAX)),
            int(m.get("mention_window_seconds", self._DEFAULT_WINDOW)),
            int(m.get("mention_reset_seconds",  self._DEFAULT_RESET)),
            int(m.get("mention_timeout_seconds",self._DEFAULT_TIMEOUT)),
        )

    def record(self, user_id: int) -> None:
        """Call this every time the bot is @mentioned by this user."""
        max_m, window, _, _ = self._cfg()
        now = time.time()
        dq = self._mentions[user_id]
        dq.append(now)
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()

    def check(self, user_id: int) -> str | None:
        """
        Returns:
          "warn"    — first breach, issue a warning
          "timeout" — already warned recently, escalate to mute
          None      — within limits, do nothing
        """
        max_m, window, reset, timeout_dur = self._cfg()
        now = time.time()
        dq = self._mentions[user_id]
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) <= max_m:
            # Within limits. If they were warned but have been quiet long enough, reset.
            if self._warned_at[user_id] and now - self._warned_at[user_id] > reset:
                self._warned_at[user_id] = 0.0
                self._mentions[user_id].clear()
            return None

        # Breach detected
        last_warn = self._warned_at[user_id]
        if last_warn == 0.0 or now - last_warn > reset:
            # First breach (or long time since last warning) → warn
            self._warned_at[user_id] = now
            return "warn"
        else:
            # Already warned recently → timeout
            self._warned_at[user_id] = now   # reset so next cycle starts fresh
            self._mentions[user_id].clear()
            return "timeout"

    def timeout_duration(self) -> int:
        return self._cfg()[3]


MENTION_RL = MentionRateLimiter()
