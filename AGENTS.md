# AGENTS.md — Guide for AI Agents

This document is for autonomous AI agents (e.g., Cursor, Cline, Aider, Claude Code) working on this codebase. If you're a human, read `README.md` instead.

---

## Project summary

**Clawy** is an autonomous Discord moderation bot powered by local Ollama LLMs. It combines rule-based automation (Python) with AI judgment (Ollama) for moderation and chat.

**Core principle:** Most features do NOT use the LLM. Only moderation judgment and chat use Ollama. Everything else is pure Python + SQLite + Discord API.

**Deployment target:** Can run headless without GPU. Ollama is optional — the bot degrades gracefully.

---

## Architecture overview

```
Discord Message
      ↓
Prefilter (Python — no LLM)
  ├─ Hard blocklist match → Execute action
  ├─ Spam rate breach → Warn/timeout
  ├─ Protected user → Skip punishment
  └─ Pass → LLM moderation (if Ollama is reachable)
      ↓
LLM Moderation (Ollama)
  Returns: JSON action object
      ↓
Executor (Python guardrails)
  Validates, enforces limits, logs
      ↓
Discord API execution
```

**Chat path (separate):**
```
@mention or direct address
      ↓
Check Ollama health
  ├─ Offline → Send "I am resting" fallback message
  └─ Online → LLM generates reply using persona + chat memory
```

---

## File structure

```
discord-bot/
├── main.py                    # Entry point — registers cogs
├── config/
│   ├── config.yaml            # Main config (guild_id, owner_id, modes, rate limits, gating)
│   ├── personas.json          # Persona definitions (hot-reloadable)
│   └── role_rules.json        # Activity-based role assignment rules
├── core/                      # Internal logic — no Discord API calls here
│   ├── config.py              # Loads YAML + .env, exposes RuntimeState
│   ├── persona.py             # PersonaManager (reads/writes personas.json)
│   ├── store.py               # SQLite layer (moderation + chat + activity tables)
│   ├── ollama_client.py       # Async HTTP client for Ollama API
│   ├── prompts.py             # Builds LLM system/user prompts
│   ├── prefilter.py           # Fast rule-based pre-LLM filter
│   ├── executor.py            # Executes actions with guardrails
│   ├── tracking.py            # In-memory spam and mention rate limiters
│   └── gating.py              # Quiet hours + chat role allowlist (pure logic)
└── cogs/                      # Discord event handlers
    ├── _common.py             # CleanCommandCog base + ack / reply_permanent helpers
    ├── moderation.py          # Main on_message listener (mod + chat router)
    ├── admin.py               # All admin commands (!pause, !mode, !kick, etc.)
    ├── members.py             # Welcome DM on member join
    ├── move.py                # Webhook-based message moving
    ├── sleep.py               # !sleep / !wake with auto-wake timer
    └── roles.py               # Activity-based role assignment engine
```

---

## What uses the LLM (Ollama)

| Feature | Uses LLM? |
|---|---|
| Moderation decision (warn/delete/timeout) | ✅ Yes — only if Ollama is reachable |
| Chat replies | ✅ Yes — only if Ollama is reachable |
| Proactive (unsolicited) replies | ✅ Yes — dice roll + `proactive_reply_chance` |
| Move messages | ❌ No — pure Discord webhooks |
| Rate limiting (spam, mentions) | ❌ No — sliding window counters |
| Role auto-assignment | ❌ No — SQL activity queries + JSON rules |
| Hard blocklist enforcement | ❌ No — string matching |
| Admin commands (!kick, !ban, !mute, !help, etc.) | ❌ No — Discord API |
| Strike counting | ❌ No — SQL aggregation |
| Sleep mode | ❌ No — state flag + presence updates |
| Quiet hours | ❌ No — timezone-aware window check |
| Chat role allowlist | ❌ No — role name membership |

**Ollama is optional.** If unreachable:
- Prefilter-based moderation (blocklist, spam) still works
- LLM moderation is skipped — bot becomes pure rule-based
- Chat requests get a polite "I am resting" message

---

## Key design patterns

### 1. Separation of concerns

**Moderation memory** (who was warned/kicked) is NEVER used for chat.  
**Chat memory** (conversation history) is NEVER used for moderation.  
They are in separate SQLite tables and never cross-reference.

### 1b. Discord identity vs persona

The bot has TWO identity layers that must never be conflated:

- **Discord identity** = the bot's user account in Discord. Fixed at bot creation. Has a username, an ID, a server nickname. NEVER change this programmatically — it's the bot's actual identity.
- **Persona** = the voice/character the bot speaks as. Swappable via `!persona`. Defined in `personas.json`. Each persona has a `name` field (display name like "Seraphael") and a `key` (internal id like "seraphael" or "librarian").

When matching whether a message addresses the bot, check against:
1. `bot.user.display_name` (Discord nickname)
2. `bot.user.name` (Discord global username)
3. `PERSONAS.active_name()` (active persona's display name)

A persona's `key` and `name` may differ — e.g. `key="librarian"` `name="Margot"`. Always use `name` for user-facing matching, `key` for config/commands.

### 2. Hot-reloadable configs

- `personas.json` — `!persona reload`
- `role_rules.json` — `!roles reload`
- Both reload without restarting the bot.

### 3. Graceful degradation

When Ollama is offline:
- `health()` check returns `False`
- Moderation LLM path is skipped (prefilter still runs)
- Chat sends a fallback message instead of erroring

### 4. Admin-only dangerous actions

The LLM can NEVER kick or ban autonomously. If it tries:
- The executor intercepts and flags it to the admin log channel
- Human reads the flag, decides, then executes manually via `!kick` or `!ban`

Timeouts are capped at `max_autonomous_timeout_seconds` (default: 10 min).

### 5. Clean command handling

Every cog that registers admin `!commands` inherits from `CleanCommandCog`
(in `cogs/_common.py`) instead of `commands.Cog`. This gives uniform behavior
for all admin commands:

- **Source cleanup:** the invoking `!command` message is auto-deleted before
  the command body runs (via `cog_before_invoke`). Non-authorized users also
  get their command deleted (via a `cog_check` override). Failed parse (bad
  args, unresolvable member) triggers deletion in `main.py`'s
  `on_command_error`.
- **`ack(ctx, text, linger=6)`** — transient reply that self-deletes. Posts
  in the source channel. For "Paused", "Mode set to X", usage hints, errors.
- **`reply_permanent(ctx, text)`** — informational reply. Routes to
  `CFG.log_channel_id` when set and writable (falls back to source channel).
  A "Sent to #log." breadcrumb stays briefly in source. For `!diag`, `!whois`,
  `!strikes`, `!perms`, `!persona` listings, etc.

**To add a new gated command:**
```python
class MyCog(CleanCommandCog):
    def is_authorized(self, ctx): return _is_admin(ctx)  # override

    @commands.command(name="foo")
    async def foo(self, ctx, arg=""):
        if not arg:
            await ack(ctx, "Usage: !foo <arg>")
            return
        await reply_permanent(ctx, f"Processed {arg}...")
```

Never write `await ctx.reply(...)` or `await ctx.send(...)` directly in an
admin cog — always route through `ack()` or `reply_permanent()`.

### 6. Chat gating — quiet hours + role allowlist

The chat pipeline has two independent gates in addition to the Ollama health
check. Both are pure logic in `core/gating.py`:

- **`in_quiet_hours()`** — True during a configured time window. Handles
  midnight-crossing windows (e.g. 23:00–07:00). IANA timezone via `zoneinfo`.
  When True, chat replies AND proactive replies are suppressed. Moderation,
  prefilter, blocklist, rate-limiting, and the role engine are NOT affected.
- **`is_chat_allowed(author)`** — True if the author has a role listed in
  `CFG.chat_allowed_roles`. Empty list = everyone allowed. When False, chat
  and proactive replies are skipped silently. Moderation still runs.

Gates are consulted in `cogs/moderation.py` in two places: before `_chat()`
is called (direct address / @mention path) and inside `_moderation_llm`'s
proactive-throttle block (so unsolicited replies respect the same gates).
Session overrides for both live in `RuntimeState` and are mutated by the
`!quiet` and `!chatroles` commands.

---

## Database schema (SQLite)

**File:** `data/bot.db` (WAL mode, async-safe)

**Moderation tables:**
- `users_seen` — User identity cache (user_id, display_name, first/last seen, msg_count)
- `mod_events` — Append-only log of every moderation action (warn, timeout, kick, ban, delete)
- `bot_actions` — Non-moderation actions (moves, role grants, welcomes)

**Chat tables:**
- `chat_turns` — Per-user rolling conversation history (role: user/assistant, content, ts)
- `chat_notes` — Reserved for future long-term summaries (unused currently)

**Activity tables:**
- `activity_log` — Raw message events (user_id, channel_id, guild_id, ts)
- `role_grants` — Tracks which role rules have already fired for which users

**Meta:**
- `kv` — Key-value store for misc state

---

## Config reference (config.yaml)

```yaml
guild_id: 0          # REQUIRED — Discord server ID
owner_id: 0          # REQUIRED — owner's Discord user ID
log_channel_id: 0    # Private admin log channel (0 = disabled)
mode: "chat_and_moderate"  # moderate_only | chat_and_moderate | chat_only

database:
  path: "data/bot.db"
  chat_keep_last_turns: 50
  chat_context_turns: 8

protected_roles:     # Never punished by the bot
  - "Admin"
  - "Moderator"

ignored_channels:    # Bot reads/writes nothing
  - "staff-only"

ollama:
  model: "qwen3:8b"
  temperature: 0.6
  num_ctx: 8192
  timeout_seconds: 45
  think: false                   # run reasoning trace? false = fast direct answers

moderation:
  spam_threshold: 6              # messages
  spam_window_seconds: 10
  mention_max: 4                 # @mentions
  mention_window_seconds: 30
  mention_timeout_seconds: 300
  max_autonomous_timeout_seconds: 600  # Hard cap on LLM-issued mutes
  blocklist_enabled: false       # OPT-IN: default off. See config/blocklist.json.example
  blocklist_file: "config/blocklist.json"  # path to word list (only if enabled)
  kick_strike_threshold: 2
  ban_strike_threshold: 4
  proactive_reply_chance: 0.0    # 0.0 = off, 0.03 = 3% per message
  proactive_reply_cooldown_seconds: 300

chat:
  # Role allowlist for chat replies. Empty = everyone chats.
  # Moderation runs regardless of this list.
  allowed_roles: []
    # - "Regular"
    # - "VIP"
  quiet_hours:
    enabled: false
    timezone: "Europe/Berlin"    # IANA tz name
    start: "23:00"
    end: "07:00"                 # wraps midnight correctly

allowed_actions:     # What the LLM can pick
  - reply
  - delete
  - warn
  - timeout
  - assign_role
  - remove_role
  - ignore
  # kick and ban are intentionally excluded
```

---

## Docker deployment contract

The container is configured through a three-layer precedence chain:

```
config/config.yaml  <  /app/.env  <  process environment variables
    (lowest)                             (highest)
```

**How it works (see `docker-entrypoint.sh`):**

1. On boot, missing files in `/app/config/` are seeded from `/app/defaults/config/`.
2. `/app/.env` is parsed and each key exported into the process environment.
3. A Python block rewrites `config.yaml` with any values set via supported env
   vars — see the table below. Keys not set in env are left untouched.
4. `core/config.py` then reads `config.yaml` normally. It has no knowledge of
   this mechanism and requires no changes.

**Supported env-var overrides:**

| Env var | Target in config.yaml | Type |
|---|---|---|
| `GUILD_ID` | `guild_id` | int |
| `OWNER_ID` | `owner_id` | int |
| `LOG_CHANNEL_ID` | `log_channel_id` | int |
| `BOT_MODE` | `mode` | string (enum) |
| `OLLAMA_MODEL` | `ollama.model` | string |

**Not written into config.yaml:** `DISCORD_TOKEN` and `OLLAMA_URL`. These are
read from the process environment directly by `core/config.py`.

**Critical invariants for agents modifying this flow:**

- **Never exit the container on missing `DISCORD_TOKEN`.** The entrypoint
  idle-loops and polls every 10s. This keeps TrueNAS / Portainer / compose
  managers from crash-looping, and lets users drop a `.env` onto the volume
  without a restart.
- **Never add `set -e` at the top of `docker-entrypoint.sh`.** A read-only
  config mount causes `cp` to fail; with `set -e` that kills the container
  before the useful error is logged. Errors are handled per-step instead.
- **Never write `DISCORD_TOKEN` or `OLLAMA_URL` into `config.yaml`.** Token
  must never land on disk inside the config volume (secret leakage risk), and
  `OLLAMA_URL` has no matching key in `config.yaml` schema — it's purely env.
- **If you add a new env-override key**, update (a) the Python `set_*` calls
  in `docker-entrypoint.sh`, (b) the "Supported overrides" comment block in
  the same file, (c) the table in `DOCKER.md`, (d) the section in
  `.env.example`, and (e) the table above.

---

## Testing without Ollama

To run the bot on a headless server without GPU:

1. **Set `mode: moderate_only` or `mode: chat_and_moderate`**
2. **Don't start Ollama** — or point `OLLAMA_URL` to a nonexistent address
3. **Enable the blocklist** in `config.yaml` (`blocklist_enabled: true`) and populate `config/blocklist.json` for zero-tolerance words
4. **Enable role rules** in `role_rules.json` for activity-based role grants
5. **Use admin commands** (`!kick`, `!ban`, `!mute`) for manual moderation

The bot will:
- ✅ Enforce hard blocklist (instant delete + timeout)
- ✅ Rate-limit spam and @mentions
- ✅ Auto-assign roles based on activity
- ✅ Log everything to the admin log channel
- ✅ Move messages via `!moveto`
- ✅ Respond to all admin commands
- ❌ Skip LLM-based moderation judgment
- ❌ Reply "I am resting" to chat requests

---

## Common tasks

### Add a new moderation rule (no LLM) — optional blocklist

The blocklist is OFF by default. To enable:
1. Copy `config/blocklist.json.example` to `config/blocklist.json`
2. Add words/phrases to the JSON
3. Set `moderation.blocklist_enabled: true` in `config.yaml`
4. Restart the bot

The blocklist is pure Python — no LLM involved. Matching messages are deleted and user is auto-muted.

### Add a new role assignment rule

Edit `config/role_rules.json`, add a rule block, run `!roles reload`.

### Change the persona

Edit `config/personas.json`, add a new persona or mood, run `!persona reload`.

### Add a new admin command

Add a `@commands.command()` method inside `AdminCog` (or any cog inheriting
from `CleanCommandCog`). It auto-registers.

Use the shared helpers from `cogs/_common.py` for replies — never `ctx.reply`:

```python
from ._common import ack, reply_permanent

@commands.command(name="foo")
async def foo_cmd(self, ctx, arg: str = "") -> None:
    """Short help shown by !help foo. Keep it under ~10 lines.
    Usage: !foo <arg>
    """
    if not arg:
        await ack(ctx, "Usage: `!foo <arg>`")  # transient, source channel, 6s
        return
    await reply_permanent(ctx, f"Processed {arg}")  # informational, -> log channel
```

If your new command should appear in `!help`, also add an entry to the
`groups` list in `help_cmd` in `cogs/admin.py`.

### Add a new autonomous feature (no LLM)

1. Create a new cog in `cogs/`
2. Register it in `main.py` with `await bot.load_extension("cogs.yourcog")`
3. Use `@commands.Cog.listener()` for event hooks
4. Use `STORE` for database access
5. Use `CFG` for config access

### Debug Ollama issues

Check `!diag` output — it shows Ollama reachability + latency.

If health check fails:
```python
await OLLAMA.health()  # Returns True/False
```

If LLM calls time out, increase `ollama.timeout_seconds` in config.

---

## Critical rules for agents

### DO:
- Use `STORE` methods for all database access (never raw SQL)
- Check `CFG.state.paused` and `CFG.state.sleeping` before autonomous actions
- Check `in_quiet_hours()` and `is_chat_allowed()` before chat/proactive replies
- Use `await OLLAMA.health()` before calling the LLM
- Log all moderation actions to `log_channel_id` via `_log_action()`
- Respect `protected_roles` and `owner_id` in all punishment logic
- Keep chat memory separate from moderation memory
- Inherit from `CleanCommandCog` in any cog that registers `!commands`
- Use `ack()` and `reply_permanent()` from `cogs/_common.py` — never `ctx.reply`

### DON'T:
- Let the LLM kick or ban autonomously (flag for human review instead)
- Block the event loop with sync I/O (use `asyncio.to_thread` if needed)
- Write to `/mnt/user-data/uploads` or other read-only mounts
- Use localStorage/sessionStorage in React artifacts (not supported)
- Assume Ollama is always available
- Apply chat gates (quiet hours, allowlist) to moderation — gates are
  chat-only. Moderation must always run regardless.

### NEVER:
- Execute SQL directly — always use `STORE` methods
- Cross moderation and chat data (separate tables for a reason)
- Skip the executor guardrails when applying actions
- Hard-code Discord IDs in the code (use config.yaml)
- Place `think` inside the Ollama `options` block — it's a top-level
  request field. Putting it under `options` is silently ignored.

---

## Performance notes

**SQLite:**
- WAL mode enabled — concurrent reads + one writer
- All writes are single-row, indexed
- Prune old activity logs every 10 min (role engine sweep)

**Discord API:**
- Rate limits are handled by discord.py automatically
- Webhooks used for message moving (preserves author identity)
- Typing indicator shows during LLM calls

**Ollama:**
- Calls are async with timeout
- Model is loaded once, stays in VRAM until Ollama restarts
- `num_ctx: 8192` is enough for Discord context (keep it low to save VRAM)

---

## Debugging checklist

**Bot doesn't respond:**
1. Check `!diag` — is Ollama reachable?
2. Verify `guild_id` matches the server
3. Check Message Content Intent is enabled in Discord Developer Portal

**Moderation not working:**
1. Is `mode` set to `moderate_only` or `chat_and_moderate`?
2. Is Ollama running? (`await OLLAMA.health()`)
3. Is `blocklist_enabled: true` and is `config/blocklist.json` populated? (fallback if LLM is down)

**Role rules not firing:**
1. Run `!roles` — are rules enabled?
2. Check `activity_log` table has recent entries
3. Run `!roles check @user` manually
4. Verify role exists in Discord with exact name match

**Chat not working:**
1. Is `mode` set to `chat_and_moderate` or `chat_only`?
2. Is the bot being @mentioned or addressed by name?
3. Is Ollama reachable?

---

## Additional resources

- **README.md** — Full user-facing documentation
- **config/config.yaml** — Inline comments on every setting
- **config/personas.json** — Example personas with all moods
- **config/role_rules.json** — Fully annotated rule structure

---

**Last updated:** 2026-04-18  
**Python version:** 3.12+ (Docker image uses python:3.12-slim)  
**Discord.py version:** 2.x  
**Recommended Ollama model (CPU):** qwen3.5:4b (2048 ctx, temp 0.75)  
**Alternative tiny model:** qwen3.5:2b (for slower CPUs)
