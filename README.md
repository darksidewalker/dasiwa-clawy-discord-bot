# Clawy — Autonomous Discord Bot

An autonomous Discord bot powered by a locally-hosted Ollama model.
Clawy moderates your server, chats in a configurable persona, remembers users,
moves messages between channels, and rate-limits people who spam her.
Everything runs on your own machine. No cloud, no API costs, no data leaving your server.

### Avatar:
![avatar-clawy.png](assets/avatar-clawy.png)
### Banner:
![banner-clawy.png](assets/banner-clawy.png)
---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [First-time configuration](#3-first-time-configuration)
4. [Getting your Discord credentials](#4-getting-your-discord-credentials)
5. [Inviting the bot](#5-inviting-the-bot)
6. [Starting the bot](#6-starting-the-bot)
7. [Bot modes](#7-bot-modes)
8. [Persona & mood system](#8-persona--mood-system)
9. [Moderation system](#9-moderation-system)
10. [Message moving](#10-message-moving)
11. [Rate limiting & anti-spam](#11-rate-limiting--anti-spam)
12. [Memory & database](#12-memory--database)
13. [All admin commands](#13-all-admin-commands)
14. [Full config reference](#14-full-config-reference)
15. [Recommended Ollama models](#15-recommended-ollama-models)
16. [File layout](#16-file-layout)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Requirements

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — fast Python package manager (used by the installer)
- **[Ollama](https://ollama.com)** — runs the local AI model
- A Discord bot token (see section 4)

---

## 2. Installation

Run the installer once. It creates a virtual environment, installs all Python
dependencies, and checks that Ollama is reachable.

**Windows:**
```
install.bat
```

**Linux / macOS:**
```bash
chmod +x install.sh
./install.sh
```

The installer will:
- Check that `uv` and `ollama` are on your PATH
- Create a `.venv` folder inside the project using `uv`
- Install all packages from `requirements.txt` into the venv
- Remind you to fill in `.env` and `config/config.yaml`

If `uv` is not installed, get it from https://docs.astral.sh/uv/getting-started/installation/

---

## 3. First-time configuration

**Step 1 — create your `.env` file:**
```
copy .env.example .env       (Windows)
cp .env.example .env         (Linux / macOS)
```
Open `.env` and paste your Discord bot token:
```
DISCORD_TOKEN=your-token-here
OLLAMA_URL=http://localhost:11434
```

**Step 2 — edit `config/config.yaml`:**

At minimum, set these three values:
```yaml
guild_id: 123456789012345678     # your server ID
owner_id: 987654321098765432     # your personal Discord user ID
log_channel_id: 111122223333444  # channel where Clawy logs her actions (optional, 0 = off)
```

Everything else has sensible defaults. See section 14 for the full reference.

**Step 3 — pull an Ollama model:**
```bash
ollama pull qwen3.5:4b
```
See section 15 for model recommendations per GPU.

Then update the model name in `config/config.yaml`:
```yaml
ollama:
  model: "qwen3.5:4b"
  temperature: 0.75
  num_ctx: 512
  timeout_seconds: 20
```

---

## 4. Getting your Discord credentials

**Bot token:**
1. Go to https://discord.com/developers/applications
2. Open your application → **Bot** tab
3. Click **Reset Token** and copy it
4. On the same page, scroll to **Privileged Gateway Intents** and enable:
   - ✅ **Message Content Intent**
   - ✅ **Server Members Intent**

**Your owner ID (your personal Discord user ID):**
1. Discord → User Settings → **Advanced** → enable **Developer Mode**
2. Right-click your own name anywhere → **Copy User ID**

**Server (guild) ID:**
1. Developer Mode must be on (see above)
2. Right-click your server icon in the sidebar → **Copy Server ID**

**Log channel ID** (optional but recommended):
1. Create a private channel visible only to admins/staff
2. Right-click that channel → **Copy Channel ID**
3. Paste as `log_channel_id` in `config/config.yaml`

---

## 5. Inviting the bot

In the Discord Developer Portal → your app → **OAuth2** → **URL Generator**:

Scopes: `bot`

Bot permissions to enable:
- Manage Roles
- Manage Channels
- **Manage Webhooks** ← required for `!moveto`
- Kick Members
- Ban Members
- Moderate Members (for timeouts)
- Manage Messages
- Read Message History
- Send Messages
- View Channels

Copy the generated URL, open it in your browser, select your server, authorize.

---

## 6. Starting the bot

After installation, use the start script every time:

**Windows:**
```
start.bat
```

**Linux / macOS:**
```bash
./start.sh
```

The script activates the virtual environment and runs `main.py`.
Press `Ctrl+C` to stop the bot.

---

## 7. Bot modes

Clawy has three operating modes. Switch at any time with `!mode`.

| Mode | What Clawy does |
|---|---|
| `moderate_only` | Silent watcher. Reads all messages, enforces rules, never chats unless issuing a warning. |
| `chat_and_moderate` | **Default.** Moderates AND replies to @mentions or direct questions in persona. |
| `chat_only` | Chats freely when addressed, completely ignores moderation. Useful for testing personas. |

Clawy is "directly addressed" when someone @mentions her, or when a message
starts with her display name (e.g. "Clawy, what do you think...").

Mode changes from `!mode` are session-only and reset on restart.
To make a mode permanent, change `mode:` in `config/config.yaml`.

---

## 8. Persona & mood system

### How identity works

The bot has **two layers of identity**:

| Layer | Where it's set | Changes? |
|---|---|---|
| **Discord identity** (username, avatar, bot user ID) | Discord Developer Portal + server nickname | Fixed — you set it once when creating the bot |
| **Persona** (the voice/character speaking) | `config/personas.json` | Swappable at runtime via `!persona <key>` |
| **Mood** (tone variation within a persona) | `config/personas.json` → `moods` | Swappable at runtime via `!mood <name>` |

**The bot's Discord name does not change** when you switch personas. If you named your bot "Clawy" in Discord, it stays "Clawy" forever — that's its identity. Personas are different *voices/characters* the bot can speak as.

Users can address the bot using **either** the Discord name **or** the active persona's name. So if your bot is named "Clawy" in Discord and the active persona is "Seraphael":
- `@Clawy hello` ✓ works (Discord mention)
- `Clawy, what's up?` ✓ works (Discord name)
- `Seraphael hello` ✓ works (active persona name)
- After `!persona nyx` → `Nyx hello` ✓ works (new persona name)
- `Seraphael hello` ✗ no longer works (not the active persona)

### File structure

### Structure of personas.json

```json
{
  "active_persona": "clawy",
  "active_mood": "neutral",
  "personas": {
    "clawy": {
      "name": "Clawy",
      "description": "Short description shown in !persona list",
      "base": "Core personality prompt. Always active.",
      "moods": {
        "neutral": "Tone instruction for neutral mood.",
        "stern":   "Tone instruction for stern mood."
      }
    }
  }
}
```

The final system prompt sent to the LLM is always: `base` + `moods[active_mood]`.
Changes persist to disk automatically and survive restarts.

### Bundled personas

| Key | Name | Description |
|---|---|---|
| `clawy` | Clawy | Ancient demon guardian. Alluring, commanding, cheeky. Dark realm gatekeeper. |
| `nyx` | Nyx | Dry-witted, brief, fair. Warm to regulars, cool to rule-breakers. |
| `librarian` | Margot | Patient, precise, gently pedantic. References rules clearly. |

### Clawy's moods

| Mood | Vibe |
|---|---|
| `neutral` | Composed, faintly amused, commanding. Default. |
| `seductive` | Magnetic, slow-burning, dangerous warmth. |
| `cheeky` | Playful mockery, raised eyebrow, ancient amusement. |
| `stern` | Cold, absolute, consequences implied. No softening. |
| `hungry` | Predatory stillness. Senses weakness. Barely contained. |
| `amused` | Theatrical delight. Something actually surprised her. |
| `weary` | Centuries of the same mistakes. Tired but still sharp. |

### Commands

```
!persona              — list all personas with descriptions and moods
!persona clawy        — switch to Clawy
!persona nyx          — switch to Nyx
!persona reload       — reload personas.json from disk after manual edits
!mood                 — show active mood and all available options
!mood seductive       — switch mood
!mood stern           — switch mood
```

### Adding your own persona

Open `config/personas.json`, add a new entry under `"personas"`, save, run
`!persona reload` in Discord. No restart needed.

---

## 9. Moderation system

Every message goes through three layers in order:

```
Message
  │
  ▼
Pre-filter (fast, no LLM)
  Checks: spam rate, blocklist, bots, ignored channels, protected users
  │
  ├─ Rule matched → execute directly
  │
  ▼
Ollama LLM
  Sees: message content, author, channel context, strike count, flags
  Returns: JSON action object
  │
  ▼
Executor (guardrails)
  Validates: allowed_actions list, protection, strike thresholds, permissions
  Logs: every action to SQLite + log channel
```

### Actions the LLM can choose autonomously

| Action | Effect |
|---|---|
| `ignore` | Nothing. Message passes through. |
| `reply` | Clawy responds in character. |
| `warn` | Warning posted in channel. Strike added. |
| `delete` | Message deleted. Strike added. |
| `timeout` | User muted. Strike added. Default duration: 10 min. |
| `assign_role` | Adds a named role to the user. |
| `remove_role` | Removes a named role from the user. |

**Intentionally unavailable to the LLM:**
- `kick` — flagged for human review instead
- `ban` — flagged for human review instead

The LLM can never kick or ban autonomously. If it tries to pick either action, the system flags it in your admin log channel with context and a suggested command for you to execute manually (`!kick @user` or `!ban @user`).

### Strike system

Strikes accumulate in SQLite and persist across restarts.
They are counted over a rolling window (default: last 24 hours).
Use `!strikes @user` to see a user's current count and recent events.

### Optional word blocklist (off by default)

The bot supports an **optional** blocklist for zero-tolerance words/phrases that
instantly mute the offender. **This is off by default** — Clawy does not block
anything unless you explicitly opt in.

**To enable it:**

1. Copy the example file:
   ```bash
   cp config/blocklist.json.example config/blocklist.json
   ```

2. Edit `config/blocklist.json` and add your words/phrases:
   ```json
   {
     "words": ["slur1", "slur2"],
     "phrases": ["specific banned phrase"],
     "timeout_seconds": 600,
     "notify_user": true,
     "notify_message": "That kind of language is not tolerated here. You have been silenced."
   }
   ```

3. In `config/config.yaml`, set:
   ```yaml
   moderation:
     blocklist_enabled: true
     blocklist_file: "config/blocklist.json"
   ```

4. Restart the bot.

**How matching works:**
- `words` — whole-word match, case-insensitive (e.g. `"slur1"` matches `"SLUR1"` but not `"slur1bonus"`)
- `phrases` — substring match, case-insensitive (e.g. `"banned phrase"` matches `"a BANNED PHRASE here"`)

**What happens on match:**
- Message is deleted immediately
- User is muted for `timeout_seconds` (default 10 minutes)
- User receives a DM with `notify_message` (if `notify_user: true`)
- Action is logged to the admin log channel
- The LLM is never consulted — this is a pure-Python rule

### Protected roles

Users with a role listed in `protected_roles` are never punished by the bot,
regardless of what the LLM decides. They can still receive replies.

---

## 10. Message moving

Clawy moves messages silently using Discord webhooks. The original author's
name and avatar are preserved. Attachments and images are re-uploaded.

What regular users see:
- **Destination channel:** messages appear naturally, no header, no notice
- **Source channel:** `@username Your message was moved to #channel.` — auto-deletes after 8 seconds

What admins see in the log channel:
- Full detail: who moved it, from where, to where, how many, by which admin

The admin command message itself is deleted instantly.

### `!moveto` — reply-based (recommended)

Reply to any message in Discord, then type:
```
!moveto #destination-channel
```
To move that message plus the next N from the same author:
```
!moveto #destination-channel 3
```

### `!movelast` — user-based

Move the last N messages from a user in the current channel:
```
!movelast @username 5 #destination-channel
```

### Requirements

- Clawy needs **Manage Webhooks** in the destination channel
- Clawy needs **Manage Messages** in the source channel
- The admin running the command needs Administrator or Manage Messages permission

---

## 11. Rate limiting & anti-spam

### Message volume spam

Pre-filter, no LLM. Triggers if a user sends more than `spam_threshold`
messages within `spam_window_seconds`.

Default: **6 messages in 10 seconds** → warning issued.

### @mention spam

Tracked separately from message volume. If a user pings Clawy too often:

| Breach | What happens |
|---|---|
| **1st** | Clawy warns the user in-channel. Message is ignored. Warning auto-deletes after 12s. |
| **2nd** (within `mention_reset_seconds`) | User is timed out for `mention_timeout_seconds`. Notice auto-deletes after 12s. |
| After timeout | Slate wiped. User gets a fresh allowance. |

Default limits: **4 pings per 30 seconds**. Mute duration: **5 minutes**.

Protected users (admins, owner, protected roles) are completely exempt.

---

## 12. Memory & database

SQLite at `data/bot.db` (WAL mode, async-safe, created automatically).
Moderation memory and chat memory are in separate tables and never mixed.

### Moderation memory

| Table | Content |
|---|---|
| `users_seen` | Display name, first/last seen, message count |
| `mod_events` | Every action: type, reason, source (LLM/prefilter), timestamp |
| `bot_actions` | Moves, role changes, welcomes |

### Chat memory

| Table | Content |
|---|---|
| `chat_turns` | Rolling conversation history per user (pruned to `chat_keep_last_turns`) |

The bot feeds the last `chat_context_turns` turns (default: 8) into the LLM
when chatting so Clawy remembers what was said earlier in the conversation.

### Memory commands

```
!recall @user    — show last 10 chat turns Clawy remembers with this user
!forget @user    — wipe chat memory for this user (mod history untouched)
!whois @user     — show moderation profile: first seen, last seen, message count
!strikes @user   — show strike count and last 5 moderation events
```

---

## 13. All admin commands

All commands require **Administrator** permission or being `owner_id`.
Regular users get no response when they try — their `!command` message is
silently deleted.

**Quick tip:** run `!help` in Discord for a live command list, or
`!help <command>` for usage details of a specific command.

**Where output goes:** Transient confirmations ("Paused", "Mode set to X",
usage hints, errors) appear briefly in the channel where you typed, then
self-delete after ~6 seconds. Informational output (`!diag`, `!whois`,
`!strikes`, `!perms`, listings) is routed to the configured log channel
(`log_channel_id`) so regular users don't see admin diagnostics — with a
brief "Sent to #log." breadcrumb where you ran the command. If no log
channel is set or Clawy can't write there, output falls back to the source
channel.

### Bot control / kill switch

| Command | Description |
|---|---|
| `!pause` | Disable all autonomous actions (kill switch) |
| `!resume` | Re-enable autonomous actions |
| `!sleep` | Sleep indefinitely (ignores everything except admin commands) |
| `!sleep 30m` / `!sleep 2h` / `!sleep 1h30m` | Sleep for a duration, auto-wake afterwards |
| `!wake` | Wake immediately |
| `!sleepstatus` | Show sleep state and time-until-wake |
| `!diag` | Health check: Ollama, model, mode, persona, gating, DB |

### Mode & persona

| Command | Description |
|---|---|
| `!mode` | Show current mode and options |
| `!mode moderate_only` | Moderate only, no chat (session) |
| `!mode chat_and_moderate` | Full mode — default (session) |
| `!mode chat_only` | Chat only, no moderation (session) |
| `!persona` | List all personas with descriptions + moods |
| `!persona <key>` | Switch persona — e.g. `!persona nyx` |
| `!persona reload` | Reload personas.json from disk |
| `!mood` | Show active mood and available options |
| `!mood <n>` | Switch mood — e.g. `!mood stern` |

### Model & thinking

| Command | Description |
|---|---|
| `!model` | Show current Ollama model |
| `!model <n>` | Switch model for session — e.g. `!model qwen3:14b` |
| `!think` | Show current thinking state |
| `!think on` / `!think off` | Toggle Ollama reasoning trace |
| `!think reset` | Drop session override, use YAML value |

### Chat gating

Control *when* and *who* Clawy chats with. Moderation always runs regardless.

| Command | Description |
|---|---|
| `!quiet` | Show quiet-hours status + window |
| `!quiet on` / `!quiet off` | Enable/disable quiet hours |
| `!quiet set 23:00 07:00 Europe/Berlin` | Set window (session) |
| `!quiet reset` | Drop overrides, use YAML |
| `!chatroles` | Show chat role allowlist |
| `!chatroles add <role>` / `!chatroles remove <role>` | Manage allowlist |
| `!chatroles clear` | Empty allowlist — everyone can chat |
| `!chatroles reset` | Drop override, use YAML |
| `!proactive` | Show proactive-reply chance |
| `!proactive 0.03` | Set to 3% per eligible message |
| `!proactive off` | Disable proactive replies |
| `!proactive reset` | Drop override, use YAML |

### Manual moderation

| Command | Description |
|---|---|
| `!kick @user [reason]` | Kick a member |
| `!ban @user [reason]` | Ban a member |
| `!mute @user [duration] [reason]` | Timeout (e.g. `!mute @x 30m spam`) |
| `!unmute @user` | Remove a timeout |

### User info & memory

| Command | Description |
|---|---|
| `!whois @user` | DB profile: first seen, last seen, message count |
| `!strikes @user` | Strike count + last 5 moderation events |
| `!recall @user` | Last 10 chat memory turns |
| `!forget @user` | Wipe chat memory (moderation history untouched) |

### Message moving

| Command | Description |
|---|---|
| `!moveto #channel` | Move replied message to channel |
| `!moveto #channel N` | Move replied message + up to N more from same author |
| `!movelast @user N #channel` | Move last N messages from user in this channel |

### Activity-based roles

| Command | Description |
|---|---|
| `!roles` | List loaded role rules |
| `!roles reload` | Reload `role_rules.json` from disk |
| `!roles check @user` | Immediately evaluate rules for a user |
| `!roles grants @user` | Show which rules have fired for a user |
| `!roles reset @user <rule_id>` | Clear a grant so the rule can fire again |

### Diagnostics & utilities

| Command | Description |
|---|---|
| `!help` | List all commands grouped by function |
| `!help <command>` | Show detailed usage for a specific command |
| `!perms` | Show Clawy's permissions in the current channel + role hierarchy |
| `!setlog #channel` | Set log channel for session |

---

## 14. Full config reference

`config/config.yaml`:

```yaml
# ── Discord ──────────────────────────────────────────────────────────
guild_id: 0          # REQUIRED. Your server ID.
owner_id: 0          # REQUIRED. Your Discord user ID. Bot never acts on you.
log_channel_id: 0    # Private admin/log channel ID. 0 = disabled.
command_prefix: "!"  # Prefix for all admin commands.

# ── Bot mode ─────────────────────────────────────────────────────────
mode: "chat_and_moderate"
# Options: moderate_only | chat_and_moderate | chat_only

# ── Storage ──────────────────────────────────────────────────────────
database:
  path: "data/bot.db"       # SQLite file (created automatically)
  chat_keep_last_turns: 50  # Max stored turns per user before pruning
  chat_context_turns: 8     # How many past turns fed to LLM per reply

# ── Protected roles ──────────────────────────────────────────────────
protected_roles:
  - "Admin"
  - "Moderator"

# ── Ignored channels ─────────────────────────────────────────────────
ignored_channels:
  - "staff-only"

# ── Ollama ───────────────────────────────────────────────────────────
ollama:
  model: "qwen3.5:4b"
  temperature: 0.75     # 0.0 = deterministic, 1.0 = creative
  num_ctx: 512          # Context window in tokens
  timeout_seconds: 20   # Max wait for Ollama before giving up
  think: false          # false = fast direct answers (recommended).
                        # true = run the model's reasoning trace first —
                        # much slower on CPU. Toggleable via !think.
                        # Requires Ollama >= 0.9.

# ── Moderation ───────────────────────────────────────────────────────
moderation:
  # Enable automatic muting from the hard blocklist (see blocklist_file below).
  # If false (default), the blocklist is ignored entirely — the bot will not
  # auto-mute anyone based on word matches. You can still moderate via the LLM
  # or manually with !mute / !kick / !ban.
  blocklist_enabled: false

  # Optional JSON file with words/phrases that trigger automatic muting.
  # Only read if blocklist_enabled: true above. See config/blocklist.json.example
  blocklist_file: "config/blocklist.json"

  proactive_reply_cooldown_seconds: 300
  proactive_reply_chance: 0.0
  default_timeout_seconds: 600
  strike_window_hours: 24
  spam_threshold: 6            # messages in window that trigger spam warning
  spam_window_seconds: 10

  # ---- Mention rate limit (how often a user can @mention the bot) ----
  mention_max: 4               # max @mentions allowed within the window
  mention_window_seconds: 30   # sliding window size in seconds
  mention_reset_seconds: 120   # seconds of quiet before strikes reset
  mention_timeout_seconds: 300 # mute duration on escalation (5 minutes)

# ── Chat gating ──────────────────────────────────────────────────────
chat:
  # Role allowlist — which roles can chat with Clawy.
  # Empty list = everyone can chat (default behavior).
  # Non-empty = ONLY members of those roles get chat replies.
  # Moderation applies to everyone regardless of this list.
  # Role names are case-sensitive and must match Discord exactly.
  # Runtime overrides: !chatroles add|remove|clear
  allowed_roles: []
    # - "Regular"
    # - "VIP"
    # - "Staff"

  # Quiet hours — scheduled silence for the chat pipeline.
  # During quiet hours:
  #   - Clawy ignores @mentions and direct addresses (stays silent).
  #   - Proactive replies are suppressed.
  #   - Moderation, prefilter, blocklist, rate-limiting, role engine all
  #     keep running normally.
  # Runtime overrides: !quiet on|off|set|reset
  quiet_hours:
    enabled: false
    timezone: "Europe/Berlin"    # IANA timezone name (see tzdata)
    start: "23:00"               # 24h format, HH:MM
    end: "07:00"                 # wraps midnight correctly

# ── Message moving ───────────────────────────────────────────────────
move:
  max_batch: 25   # Max messages moveable in one command

# ── Allowed LLM actions ──────────────────────────────────────────────
allowed_actions:
  - reply
  - delete
  - warn
  - timeout
  - kick
  - assign_role
  - remove_role
  - ignore
  # Uncomment to enable autonomous banning:
  # - ban
```

---

## 15. Recommended Ollama models

| GPU | VRAM | Model | Pull command |
|---|---|---|---|
| RTX 3060 - 4080 | 6-16 GB | `qwen3.5:4b` | `ollama pull qwen3.5:4b` |
| Under 6 GB VRAM | 0–6 GB | `qwen3.5:2b`  | `ollama pull qwen3.5:2b` |

**Why Qwen3?** Best open-weight model at the 8B tier for roleplay, instruction
following, and reliable JSON output. Explicitly trained for creative writing and
multi-turn dialogue.

Qwen3 has a built-in "thinking" mode that the bot disables automatically via
`/no_think` in the prompt — keeping responses fast and clean.

After pulling a new model, update `ollama.model` in config and restart.
Or switch mid-session: `!model qwen3:14b`

---

## 16. File layout

```
discord-bot/
├── install.bat / install.sh    ← run once to set up the environment
├── start.bat / start.sh        ← run every time to start the bot
├── main.py                     ← entry point
├── requirements.txt
├── .env.example                ← copy to .env and fill in your token
│
├── config/
│   ├── config.yaml             ← main configuration
│   ├── personas.json           ← personas and moods
│   ├── role_rules.json         ← activity-based role assignment rules
│   └── blocklist.json          ← optional zero-tolerance word list
│
├── core/
│   ├── config.py               ← loads config.yaml and .env
│   ├── persona.py              ← persona manager (reads/writes personas.json)
│   ├── store.py                ← SQLite database layer
│   ├── ollama_client.py        ← async Ollama HTTP client
│   ├── prompts.py              ← builds LLM system/user prompts
│   ├── prefilter.py            ← fast rule-based pre-filter
│   ├── executor.py             ← executes actions with guardrails
│   ├── tracking.py             ← in-memory spam and mention rate limiters
│   └── gating.py               ← quiet hours + chat role allowlist helpers
│
├── cogs/
│   ├── _common.py              ← shared CleanCommandCog base + ack / reply helpers
│   ├── moderation.py           ← main message listener and router
│   ├── admin.py                ← all !commands
│   ├── members.py              ← welcome on member join
│   ├── move.py                 ← webhook-based message moving
│   ├── sleep.py                ← !sleep / !wake with auto-wake timer
│   └── roles.py                ← activity-based role assignment engine
│
└── data/
    └── bot.db                  ← SQLite database (auto-created)
```

---

## 17. Troubleshooting

**Bot is online but does not respond**
Check `!diag` — is Ollama reachable? Confirm `guild_id` is correct and
that Message Content Intent is enabled in the Developer Portal.

**"I need Manage Webhooks in #channel"**
Channel Settings → Permissions → find the bot's role → enable Manage Webhooks.

**Moods list is empty after `!mood`**
Your `personas.json` has a structural problem. Validate it:
```bash
python -c "import json; d=json.load(open('config/personas.json')); print(list(d['personas'][d['active_persona']]['moods'].keys()))"
```
If it returns an empty list or errors, check for missing commas or brackets.
Then run `!persona reload`.

**Clawy refuses to stay in character ("I'm not interested")**
This is the small model's safety filter overriding the persona.
Switch to a larger model: `!model qwen3:8b`
Confirm `core/prompts.py` has `/no_think\n` at the start of `_ROLEPLAY_FRAME`.

**Ollama is timing out**
Increase `ollama.timeout_seconds`. Check `nvidia-smi` — if VRAM is full the
model spills to RAM and becomes very slow. Use a smaller quantization.

**Strikes not showing after restart**
They are persisted in `data/bot.db`. Run `!strikes @user` to confirm.
If the file doesn't exist, the database failed to initialize — check startup logs.

**Bot acts on protected users**
Role names in `protected_roles` must match exactly as they appear in Discord,
including capitalization.
