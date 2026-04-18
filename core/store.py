"""
SQLite persistence. Two logically separate stores in one file:

1. MODERATION store
   - users_seen:    lightweight identity cache (user_id, name, first_seen, last_seen, msg_count)
   - mod_events:    every moderation decision & action (strike, warn, timeout, kick, ban, delete)
   - bot_actions:   other things the bot did (move, role change, welcome, etc.)

2. CHAT store (memory the bot uses to talk to people, NOT used for moderation decisions)
   - chat_turns:    per-user rolling conversation turns
   - chat_notes:    distilled long-term notes about a user's interests/tone

These are intentionally separate tables — nothing in chat_* is used when the bot
decides to punish someone, and nothing in mod_events is used when the bot chats.
The only bridge is user_id.

Perf:
  - WAL journal mode (concurrent reads + one writer without blocking Discord event loop)
  - synchronous=NORMAL (safe with WAL, ~10x faster than FULL)
  - All writes are tiny, single-row, indexed
  - Access from the async loop is via asyncio.to_thread to avoid blocking
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA foreign_keys = ON;

-- ========================================================================
-- MODERATION STORE
-- ========================================================================

CREATE TABLE IF NOT EXISTS users_seen (
    user_id      INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    first_seen   INTEGER NOT NULL,   -- unix seconds (when bot first saw them)
    last_seen    INTEGER NOT NULL,   -- unix seconds (last message)
    msg_count    INTEGER NOT NULL DEFAULT 0,
    joined_at    INTEGER,            -- unix seconds (when they joined Discord server)
    notes        TEXT                -- optional free-text (admin-set)
);

-- mod_events is append-only. Strikes are derived from it.
CREATE TABLE IF NOT EXISTS mod_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,      -- 'warn'|'timeout'|'kick'|'ban'|'delete'|'strike'
    reason      TEXT,
    source      TEXT,               -- 'prefilter'|'llm'|'admin'|'rule:<name>'
    channel_id  INTEGER,
    message_id  INTEGER,
    extra       TEXT                -- JSON string for extras (duration, role name, ...)
);
CREATE INDEX IF NOT EXISTS idx_mod_user_ts ON mod_events(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_mod_ts      ON mod_events(ts);

-- bot_actions: non-moderation things the bot did (moves, role mgmt, welcomes)
CREATE TABLE IF NOT EXISTS bot_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    kind        TEXT NOT NULL,      -- 'move'|'welcome'|'assign_role'|'remove_role'|...
    actor_id    INTEGER,            -- who asked (admin user id) or NULL if autonomous
    target_id   INTEGER,            -- target user id if applicable
    channel_id  INTEGER,
    summary     TEXT,
    extra       TEXT                -- JSON
);
CREATE INDEX IF NOT EXISTS idx_bot_ts ON bot_actions(ts);

-- ========================================================================
-- CHAT STORE  (kept separate by convention; do not read in moderation paths)
-- ========================================================================

CREATE TABLE IF NOT EXISTS chat_turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    role       TEXT NOT NULL,     -- 'user' or 'assistant'
    content    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_user_ts ON chat_turns(user_id, ts);

-- Distilled per-user notes the bot builds up over time (optional future use)
CREATE TABLE IF NOT EXISTS chat_notes (
    user_id    INTEGER PRIMARY KEY,
    summary    TEXT,
    updated_at INTEGER NOT NULL
);

-- ========================================================================
-- ACTIVITY STORE  (message counts for role rule engine)
-- ========================================================================

-- Raw message events — used to count activity in any time window
CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_act_user_ts    ON activity_log(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_act_channel_ts ON activity_log(channel_id, ts);

-- Tracks which rules have already fired for which users (prevents re-granting)
CREATE TABLE IF NOT EXISTS role_grants (
    user_id  INTEGER NOT NULL,
    rule_id  TEXT    NOT NULL,
    granted_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, rule_id)
);

-- ========================================================================
-- META
-- ========================================================================

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()   # serialize writes from the event loop

    # ---------- lifecycle ----------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    async def init(self) -> None:
        def _do() -> None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = self._connect()
            self._conn.executescript(SCHEMA)
            # Migration: add joined_at column to existing databases that predate it
            try:
                self._conn.execute(
                    "ALTER TABLE users_seen ADD COLUMN joined_at INTEGER"
                )
            except Exception:
                pass  # column already exists — that's fine
        await asyncio.to_thread(_do)
        log.info("SQLite store ready at %s", self.path)

    async def close(self) -> None:
        def _do() -> None:
            if self._conn:
                self._conn.close()
                self._conn = None
        await asyncio.to_thread(_do)

    # ---------- low-level helpers ----------
    async def _exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        assert self._conn is not None, "store not initialized"
        async with self._lock:
            await asyncio.to_thread(self._conn.execute, sql, tuple(params))

    async def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        assert self._conn is not None
        def _do() -> sqlite3.Row | None:
            cur = self._conn.execute(sql, tuple(params))  # type: ignore[union-attr]
            return cur.fetchone()
        return await asyncio.to_thread(_do)

    async def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        assert self._conn is not None
        def _do() -> list[sqlite3.Row]:
            cur = self._conn.execute(sql, tuple(params))  # type: ignore[union-attr]
            return list(cur.fetchall())
        return await asyncio.to_thread(_do)

    # ---------- users_seen ----------
    async def touch_user(self, user_id: int, display_name: str, 
                       joined_at: int | None = None) -> None:
        """
        Record or update a user. On first encounter, captures Discord profile data
        (join date). On subsequent encounters, just updates last_seen and msg_count.
        """
        now = int(time.time())
        await self._exec(
            """
            INSERT INTO users_seen (user_id, display_name, first_seen, last_seen, msg_count, joined_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                last_seen    = excluded.last_seen,
                msg_count    = msg_count + 1
            """,
            (user_id, display_name, now, now, joined_at),
        )

    async def get_user(self, user_id: int) -> dict | None:
        row = await self._fetchone("SELECT * FROM users_seen WHERE user_id = ?", (user_id,))
        return dict(row) if row else None

    async def get_user_context(self, user_id: int) -> str:
        """
        Build a human-readable context string about a user for the LLM.
        Includes join date, activity level, and admin notes.
        """
        user = await self.get_user(user_id)
        if not user:
            return ""
        
        lines = []
        
        # Join date context
        if user.get("joined_at"):
            import datetime as _dt
            try:
                joined = _dt.datetime.fromtimestamp(user["joined_at"], tz=_dt.timezone.utc)
                days_ago = (_dt.datetime.now(_dt.timezone.utc) - joined).days
                if days_ago == 0:
                    lines.append("This user joined today.")
                elif days_ago < 7:
                    lines.append(f"This user is new, joined {days_ago} days ago.")
                elif days_ago < 30:
                    lines.append(f"This user has been here about {days_ago // 7} weeks.")
                else:
                    lines.append(f"This user has been here {days_ago // 30} months.")
            except Exception:
                pass
        
        # Activity level
        msg_count = user.get("msg_count", 0)
        if msg_count == 0:
            lines.append("They've sent no messages yet.")
        elif msg_count < 5:
            lines.append("They speak rarely.")
        elif msg_count < 20:
            lines.append("They participate occasionally.")
        elif msg_count < 100:
            lines.append("They're a regular presence.")
        else:
            lines.append(f"They're very active, with {msg_count} messages observed.")
        
        # Admin notes
        if user.get("notes"):
            lines.append(f"Admin notes: {user['notes']}")
        
        return " ".join(lines)

    # ---------- mod_events ----------
    async def log_mod_event(
        self,
        user_id: int,
        kind: str,
        reason: str = "",
        source: str = "",
        channel_id: int | None = None,
        message_id: int | None = None,
        extra: str | None = None,
    ) -> None:
        await self._exec(
            """
            INSERT INTO mod_events (ts, user_id, kind, reason, source, channel_id, message_id, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(time.time()), user_id, kind, reason, source, channel_id, message_id, extra),
        )

    async def count_strikes(self, user_id: int, window_hours: int = 24) -> int:
        cutoff = int(time.time()) - window_hours * 3600
        row = await self._fetchone(
            """
            SELECT COUNT(*) AS n FROM mod_events
            WHERE user_id = ? AND ts >= ?
              AND kind IN ('warn','timeout','kick','strike','delete')
            """,
            (user_id, cutoff),
        )
        return int(row["n"]) if row else 0

    async def recent_mod_events(self, user_id: int, limit: int = 10) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM mod_events WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(r) for r in rows]

    # ---------- bot_actions ----------
    async def log_bot_action(
        self,
        kind: str,
        actor_id: int | None = None,
        target_id: int | None = None,
        channel_id: int | None = None,
        summary: str = "",
        extra: str | None = None,
    ) -> None:
        await self._exec(
            """
            INSERT INTO bot_actions (ts, kind, actor_id, target_id, channel_id, summary, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(time.time()), kind, actor_id, target_id, channel_id, summary, extra),
        )

    # ---------- chat_turns (chat memory — intentionally kept apart) ----------
    async def add_chat_turn(self, user_id: int, channel_id: int, role: str, content: str) -> None:
        await self._exec(
            "INSERT INTO chat_turns (ts, user_id, channel_id, role, content) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), user_id, channel_id, role, content),
        )

    async def recent_chat_turns(self, user_id: int, limit: int = 12) -> list[dict]:
        rows = await self._fetchall(
            "SELECT role, content, ts FROM chat_turns WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        )
        # reverse so it's chronological
        return list(reversed([dict(r) for r in rows]))

    async def prune_chat_turns(self, user_id: int, keep_last: int = 50) -> None:
        """Keep only the most recent N turns per user."""
        await self._exec(
            """
            DELETE FROM chat_turns
            WHERE user_id = ?
              AND id NOT IN (
                SELECT id FROM chat_turns WHERE user_id = ? ORDER BY ts DESC LIMIT ?
              )
            """,
            (user_id, user_id, keep_last),
        )

    async def forget_user_chat(self, user_id: int) -> None:
        await self._exec("DELETE FROM chat_turns WHERE user_id = ?", (user_id,))
        await self._exec("DELETE FROM chat_notes WHERE user_id = ?", (user_id,))

    # ---------- activity_log ----------

    async def record_activity(self, user_id: int, channel_id: int, guild_id: int) -> None:
        """Record one message event for activity tracking."""
        await self._exec(
            "INSERT INTO activity_log (ts, user_id, channel_id, guild_id) VALUES (?,?,?,?)",
            (int(time.time()), user_id, channel_id, guild_id),
        )

    async def count_activity(
        self,
        user_id: int,
        window_seconds: int,
        channel_id: int | None = None,
    ) -> int:
        """
        Count messages from user_id in the last window_seconds.
        If channel_id is given, only count messages in that channel.
        """
        cutoff = int(time.time()) - window_seconds
        if channel_id is not None:
            row = await self._fetchone(
                "SELECT COUNT(*) AS n FROM activity_log "
                "WHERE user_id=? AND ts>=? AND channel_id=?",
                (user_id, cutoff, channel_id),
            )
        else:
            row = await self._fetchone(
                "SELECT COUNT(*) AS n FROM activity_log WHERE user_id=? AND ts>=?",
                (user_id, cutoff),
            )
        return int(row["n"]) if row else 0

    async def prune_activity(self, older_than_seconds: int = 30 * 24 * 3600) -> None:
        """Delete activity records older than N seconds. Call periodically."""
        cutoff = int(time.time()) - older_than_seconds
        await self._exec("DELETE FROM activity_log WHERE ts < ?", (cutoff,))

    # ---------- role_grants ----------

    async def has_role_grant(self, user_id: int, rule_id: str) -> bool:
        """Return True if this rule has already been granted to this user."""
        try:
            row = await self._fetchone(
                "SELECT 1 FROM role_grants WHERE user_id=? AND rule_id=?",
                (user_id, rule_id),
            )
            return row is not None
        except Exception:
            return False

    async def set_role_grant(self, user_id: int, rule_id: str) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO role_grants (user_id, rule_id, granted_at) VALUES (?,?,?)",
            (user_id, rule_id, int(time.time())),
        )

    async def clear_role_grant(self, user_id: int, rule_id: str) -> None:
        """Remove a grant record so the rule can fire again (e.g. after role removal)."""
        await self._exec(
            "DELETE FROM role_grants WHERE user_id=? AND rule_id=?",
            (user_id, rule_id),
        )

    async def user_role_grants(self, user_id: int) -> list[str]:
        """Return list of rule_ids already granted to this user."""
        rows = await self._fetchall(
            "SELECT rule_id FROM role_grants WHERE user_id=?", (user_id,)
        )
        return [r["rule_id"] for r in rows]

    # ---------- kv ----------
    async def kv_get(self, key: str) -> str | None:
        row = await self._fetchone("SELECT v FROM kv WHERE k = ?", (key,))
        return row["v"] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self._exec(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )


# singleton — path set in config
STORE = Store("data/bot.db")
