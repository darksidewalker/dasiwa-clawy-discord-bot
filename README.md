# Clawy — Autonomous Discord Bot

An autonomous Discord bot powered by a locally-hosted Ollama model.
Clawy moderates your server, chats in a configurable persona, remembers users,
moves messages between channels, hands out activity-based roles, and rate-limits
people who spam her. Everything runs on your own machine — no cloud, no API
costs, no data leaving your server.

### Avatar
![avatar-clawy.png](assets/avatar-clawy.png)

### Banner
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
8. [Persona and mood system](#8-persona-and-mood-system)
9. [Moderation system](#9-moderation-system)
10. [Activity-based roles](#10-activity-based-roles)
11. [Message moving](#11-message-moving)
12. [Rate limiting and anti-spam](#12-rate-limiting-and-anti-spam)
13. [Sleep mode and quiet hours](#13-sleep-mode-and-quiet-hours)
14. [Memory and database](#14-memory-and-database)
15. [All admin commands](#15-all-admin-commands)
16. [Full config reference](#16-full-config-reference)
17. [Recommended Ollama models](#17-recommended-ollama-models)
18. [File layout](#18-file-layout)
19. [Troubleshooting](#19-troubleshooting)

---

## 1. Requirements

- Python 3.11+ (Docker image uses 3.12)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) — fast Python package manager (used by the installer)
- [Ollama](https://ollama.com) — runs the local AI model
- A Discord bot token (see section 4)

Docker is also supported as a deployment path. See `DOCKER.md` and `QUICKSTART.md`
for container setup, including a TrueNAS-friendly compose file.

---

## 2. Installation

### Local (recommended for development)

Run the installer once. It creates a virtual environment, installs all Python
dependencies, and confirms `uv` is available.

**Windows:**
```
install.bat
```

**Linux / macOS:**
```bash
chmod +x install.sh
./install.sh
```

If `uv` is not installed, get it from
https://docs.astral.sh/uv/getting-started/installation/ and re-run the installer.

### Docker

```bash
cp .env.example .env
$EDITOR .env            # paste DISCORD_TOKEN
docker compose up -d --build
docker compose logs -f clawy
```

TrueNAS users: use `docker-compose.truenas.yml` and configure everything via
environment variables in the Apps UI — no file editing required.

---

## 3. First-time configuration

**Step 1 — create your `.env` file:**
```
copy .env.example .env       (Windows)
cp    .env.example .env      (Linux / macOS)
```
Open `.env` and paste your Discord bot token:
```
DISCORD_TOKEN=your-token-here
OLLAMA_URL=http://localhost:11434
```

**Step 2 — edit `config/config.yaml`:**

At minimum, set these three values:
```yaml
guild_id:       123456789012345678   # your server ID
owner_id:       987654321098765432   # your personal Discord user ID
log_channel_id: 111122223333444555   # private admin channel ID (optional, 0 = off)
```

Everything else has sensible defaults. See section 16 for the full reference.

**Step 3 — pull an Ollama model:**
```bash
ollama pull hermes3:3b
```
See section 17 for model recommendations per hardware tier.

Then make sure the model name in `config/config.yaml` matches:
```yaml
ollama:
  model: "hermes3:3b"
  temperature: 0.85
  num_ctx: 4096
  timeout_seconds: 60
```

---

## 4. Getting your Discord credentials

**Bot token:**
1. Go to https://discord.com/developers/applications
2. Open your application → **Bot** tab
3. Click **Reset Token** and copy it
4. On the same page, scroll to **Privileged Gateway Intents** and enable:
   - **Message Content Intent**
   - **Server Members Intent**

**Your owner ID (your personal Discord user ID):**
1. Discord → User Settings → **Advanced** → enable **Developer Mode**
2. Right-click your own name anywhere → **Copy User ID**

**Server (guild) ID:**
1. Developer Mode must be on (see above)
2. Right-click your server icon in the sidebar → **Copy Server ID**

**Log channel ID (optional but strongly recommended):**
1. Create a private channel visible only to admins/staff
2. Right-click that channel → **Copy Channel ID**
3. Paste as `log_channel_id` in `config/config.yaml`

---

## 5. Inviting the bot

In the Discord Developer Portal → your app → **OAuth2** → **URL Generator**:

Scopes: `bot`

Bot permissions to enable:
- View Channels
- Send Messages
- Read Message History
- Manage Messages
- **Manage Webhooks** (required for `!moveto`)
- Manage Roles (required for activity-based roles)
- Moderate Members (required for timeouts)
- Kick Members
- Ban Members

Copy the generated URL, open it in your browser, select your server, authorize.

After inviting, drag Clawy's role above any role she needs to manage. She cannot
assign or remove a role that sits above her own in the hierarchy.

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
Press `Ctrl+C` to stop the bot. Under Docker, use `docker compose down`.

---

## 7. Bot modes

Clawy has three operating modes. Switch at any time with `!mode`.

| Mode                | What Clawy does |
|---------------------|---|
| `moderate_only`     | Silent watcher. Reads all messages, enforces rules, never chats unless issuing a warning. |
| `chat_and_moderate` | Default. Moderates AND replies to @mentions or direct addresses in persona. |
| `chat_only`         | Chats freely when addressed, completely ignores moderation. Useful for testing personas. |

Mode changes from `!mode` are session-only and reset on restart. To make a mode
permanent, change `mode:` in `config/config.yaml`.

### How addressing works

Clawy considers herself "directly addressed" when:

- Someone @mentions her, **or**
- A message starts with her **Discord display name** (e.g. "Clawy, what do you think...")

By default she does **not** respond when called by the active persona's name —
this avoids confusion when personas are swapped at runtime. To enable persona-name
matching set `respond_to_persona_name: true` in `config/config.yaml`. With it on,
all of these work:
- `@Clawy hello`
- `Clawy, what's up?`
- `Seraphael hello` (when the active persona is Seraphael)
- After `!persona nyx` → `Nyx hello`

Recognized prefixes are: `name`, `name `, `name,`, `name?`, `name!`, `name:`.

---

## 8. Persona and mood system

### Two layers of identity

| Layer              | Where it's set                              | Changeable at runtime? |
|--------------------|---------------------------------------------|------------------------|
| Discord identity   | Discord Developer Portal + server nickname  | Fixed once             |
| Persona (voice)    | `config/personas.json`                      | `!persona <key>`       |
| Mood (tone variant)| `config/personas.json` → `moods`            | `!mood <name>`         |

The bot's **Discord name** does not change when you switch personas — that's its
permanent identity. Personas are different *voices* the bot speaks in.

### Structure of `personas.json`

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

The final system prompt sent to the LLM is `base` + `moods[active_mood]`.
Changes from `!persona` and `!mood` persist to disk automatically and survive restarts.

### Bundled personas

| Key         | Name    | Description |
|-------------|---------|---|
| `clawy`     | Clawy   | Ancient demon guardian. Alluring, commanding, cheeky. Dark realm gatekeeper. |
| `nyx`       | Nyx     | Dry-witted, brief, fair. Warm to regulars, cool to rule-breakers. |
| `librarian` | Margot  | Patient, precise, gently pedantic. References rules clearly. |

### Clawy's moods

| Mood        | Vibe |
|-------------|---|
| `neutral`   | Composed, faintly amused, commanding. Default. |
| `seductive` | Magnetic, slow-burning, dangerous warmth. |
| `cheeky`    | Playful mockery, raised eyebrow, ancient amusement. |
| `stern`     | Cold, absolute, consequences implied. No softening. |
| `hungry`    | Predatory stillness. Senses weakness. Barely contained. |
| `amused`    | Theatrical delight. Something actually surprised her. |
| `weary`     | Centuries of the same mistakes. Tired but still sharp. |

### Adding your own persona

Edit `config/personas.json`, add a new entry under `"personas"`, save, then run
`!persona reload` in Discord. No restart needed.

---

## 9. Moderation system

Every message goes through three layers in order:

```
Message
  │
  ▼
Pre-filter (fast, no LLM)
  Checks: ignored channels, bots, protected users, blocklist, spam rate
  │
  ├─ Rule matched → execute directly
  │
  ▼
Ollama LLM
  Sees: message, author, channel, recent context, strike count
  Returns: a JSON action object
  │
  ▼
Executor (guardrails)
  Validates against allowed_actions, protected roles, hierarchy, timeout cap
  Logs every action to SQLite + the log channel
```

### Actions the LLM can choose autonomously

| Action         | Effect |
|----------------|---|
| `ignore`       | Nothing. Message passes through. |
| `reply`        | Clawy responds in character. |
| `warn`         | Warning posted in channel. Strike added. |
| `delete`       | Message deleted. Strike added. |
| `timeout`      | User muted. Clamped to `max_autonomous_timeout_seconds` (default 10 min). |
| `assign_role`  | Adds a named role. |
| `remove_role`  | Removes a named role. |

**Intentionally unavailable to the LLM:**
- `kick` — flagged for human review instead
- `ban` — flagged for human review instead

If the LLM picks kick or ban, the executor blocks it and logs a recommendation in
the admin log channel. You execute manually with `!kick @user` or `!ban @user`.

### Strike system

Strikes accumulate in SQLite (`mod_events` table) and persist across restarts.
They are counted over a rolling window of `strike_window_hours` (default: 24).

Spam escalation rule: once a user accumulates `spam_strike_threshold` strikes
within the window (default 3), the next spam-rate breach **escalates** from a
warning to a `spam_timeout_seconds` mute (default 600 seconds = 10 min) plus a
delete. Note that the threshold counts **all** prior mod events (warns,
timeouts, deletes, kicks) within the window, not just spam events.

Use `!strikes @user` to inspect a user's count and last 5 events.

### Optional word blocklist (off by default)

A pure-Python rule that bypasses the LLM and instantly mutes the offender on
exact match. Off by default — Clawy does not block anything unless you opt in.

**Enable it:**

1. Copy the example file:
   ```bash
   cp config/blocklist.json.example config/blocklist.json
   ```

2. Edit `config/blocklist.json`:
   ```json
   {
     "words": ["slur1", "slur2"],
     "phrases": ["specific banned phrase"],
     "timeout_seconds": 600,
     "notify_user": true,
     "notify_message": "That kind of language is not tolerated here. You have been silenced."
   }
   ```

3. In `config/config.yaml`:
   ```yaml
   moderation:
     blocklist_enabled: true
     blocklist_file: "config/blocklist.json"
   ```

4. Restart the bot.

**Matching rules:**
- `words` — whole-word, case-insensitive (`"slur1"` matches `"SLUR1"` but not `"slur1bonus"`)
- `phrases` — substring, case-insensitive

**On match:**
- Message is deleted
- User is muted for `timeout_seconds`
- User receives a DM with `notify_message` if `notify_user: true`
- Action is logged to the admin log channel
- The LLM is never consulted

### Protected users

Users matching **any** of the following are exempt from autonomous punishment:

- The owner (`owner_id` from config)
- Anyone with a role listed in `protected_roles`

They can still receive replies. Role names in `protected_roles` are
case-sensitive and must match Discord exactly.

### Proactive replies

When idle, Clawy can occasionally jump into the conversation unprompted. Off by
default. Controlled by `moderation.proactive_reply_chance` (0.0–1.0, e.g. 0.02
for 2% per eligible message) with a per-channel cooldown of
`moderation.proactive_reply_cooldown_seconds` (default 300 s = 5 min).

Proactive replies respect quiet hours, sleep mode, the chat allowlist, and the
ignored-channels list.

You can also force her to jump in immediately with `!jumpin` (see section 15).

---

## 10. Activity-based roles

Clawy automatically grants Discord roles based on user activity. Rules live in
`config/role_rules.json` and are hot-reloadable with `!roles reload` — no
restart needed.

### How activity is counted

Clawy logs every non-bot message to a local SQLite table (`activity_log`) at the
moment it arrives. She can therefore only count messages **she has personally
witnessed while online**. Messages sent during downtime or before the bot was
installed are **not** retroactively counted. The activity log is pruned to the
last 35 days on each background sweep.

### How tenure is determined

Tenure (the `min_days_member` gate) is read directly from
`discord.Member.joined_at` — the real Discord server-join timestamp. This works
correctly even for users who joined long before Clawy was installed.

A separate optional gate, `min_days_observed`, uses `users_seen.first_seen` (the
moment Clawy first saw the user). It only counts time since the bot started
watching them.

### Evaluation passes

- **On every message** the rule engine evaluates the author's eligibility.
- **Every 10 minutes** a background sweep evaluates every member of the guild.
- Rules are evaluated from **highest tier to lowest** (sorted by `min_days_member`,
  then `count`). The first rule that fires wins; lower-tier rules in the same
  pass are skipped, and any roles listed in that rule's `remove_roles` are
  stripped. This prevents granting two competing tier roles in one pass.

### Rule schema

```json
{
  "id": "veil_keepers",
  "enabled": true,
  "description": "Free-text description of the rule.",
  "trigger": {
    "type": "message_count",
    "count": 20,
    "window_days": 30,
    "channel": null,
    "min_days_member": 5,
    "min_days_observed": null
  },
  "action": {
    "grant_role": "Veil-Keepers",
    "remove_roles": ["The Uncoded"],
    "once": true
  },
  "notify": {
    "dm": true,
    "message": "Welcome to the inner circle.",
    "channel_id": null
  }
}
```

Field reference:

| Field                | Meaning |
|----------------------|---|
| `id`                 | Unique rule ID. Used by `!roles grants` / `!roles reset`. |
| `enabled`            | Set to `false` to disable without deleting the rule. |
| `trigger.type`       | Currently only `message_count` is supported. |
| `trigger.count`      | Messages required within the window. |
| `trigger.window_days`| Rolling window in days. |
| `trigger.channel`    | Channel name to restrict counting to, or `null` = server-wide. |
| `trigger.min_days_member`  | Required Discord-server tenure in days, or `null`. |
| `trigger.min_days_observed`| Required days-since-Clawy-first-saw-them, or `null`. |
| `action.grant_role`  | Exact Discord role name. Must already exist. |
| `action.remove_roles`| List of role names to remove on grant (for tier upgrades). |
| `action.once`        | If `true`, fires only once per user. If `false`, re-fires on re-qualification. |
| `notify.dm`          | If `true`, DM the user. |
| `notify.message`     | DM body, in persona's voice. |
| `notify.channel_id`  | Optional channel ID for a public announcement. |

### Tier upgrade behavior

When a higher rule fires (e.g. `Veil-Keepers` → `Eldritch Ones`), the new role
is granted, every name in `remove_roles` is stripped, and the *grant records* of
those superseded rules are also marked in SQLite. This means that after a bot
restart, lower-tier rules will not re-fire (and DM-spam) for users who already
hold the higher role.

If a user is found to *already* hold a rule's target role (e.g. assigned
manually), the rule's grant record is backfilled silently; no DM is sent.

### Manual control

| Command                              | Effect |
|--------------------------------------|---|
| `!roles`                             | List all loaded rules with their triggers and actions. |
| `!roles reload`                      | Reload `role_rules.json` from disk. |
| `!roles check @user`                 | Immediately evaluate all rules for one user. |
| `!roles grants @user`                | Show which rules have already fired for a user. |
| `!roles reset @user <rule_id>`       | Clear a grant record so the rule can fire again. |

---

## 11. Message moving

Clawy moves messages silently using Discord webhooks. The original author's
name and avatar are preserved. Attachments and images are re-uploaded.

What regular users see:
- **Destination channel:** message appears naturally, no header or notice.
- **Source channel:** brief `@username Your message was moved to #channel.` notice that auto-deletes after a few seconds.

What admins see in the log channel:
- Full detail: who moved it, from where, to where, how many, by which admin.

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

- Clawy needs **Manage Webhooks** in the destination channel.
- Clawy needs **Manage Messages** in the source channel.
- The admin running the command needs Administrator or Manage Messages permission.
- Batch size is capped at `move.max_batch` in config (default 25).

---

## 12. Rate limiting and anti-spam

### Message volume spam

Pre-filter, no LLM. Triggers if a user sends more than `spam_threshold` messages
within `spam_window_seconds`.

Defaults: **6 messages in 10 seconds**.

Outcome on first breach: a warning (no punishment). If the user already has
`spam_strike_threshold` strikes in the rolling window, it escalates to a
`spam_timeout_seconds` mute plus a delete.

This counter is in-memory only and resets on bot restart.

### @mention spam

Tracked separately from message volume. If a user @-pings Clawy too often:

| Breach              | What happens |
|---------------------|---|
| 1st                 | Clawy warns the user in-channel. Message ignored. Notice auto-deletes. |
| 2nd within `mention_reset_seconds` | Timeout for `mention_timeout_seconds`. |
| After timeout       | Slate wiped; user gets a fresh allowance. |

Defaults: **4 pings per 30 seconds**, mute duration **5 minutes**, strike resets
after **120 s** of quiet.

Protected users (owner, protected roles) are completely exempt from both
spam systems.

---

## 13. Sleep mode and quiet hours

Two distinct silence mechanisms.

### Sleep mode (manual)

Stops Clawy from acting on anything (chat, moderation, mention rate-limit) until
a wake event. Admin commands and `!moveto` still work.

| Command           | Effect |
|-------------------|---|
| `!sleep`          | Sleep indefinitely. |
| `!sleep 30m`      | Sleep 30 minutes, then auto-wake. |
| `!sleep 2h`       | Sleep 2 hours. |
| `!sleep 1h30m`    | Sleep 1 hour 30 minutes. |
| `!wake`           | Wake immediately. |
| `!sleepstatus`    | Show sleep state and time-until-wake. |

While sleeping, Discord status changes to **Do Not Disturb** with the text
"Resting... do not disturb."

### Quiet hours (scheduled)

Suppresses **chat replies and proactive replies only** during a daily window.
Moderation, prefilter, blocklist, rate limiting, and the role engine all
keep running.

Configured under `chat.quiet_hours`:
```yaml
chat:
  quiet_hours:
    enabled: false
    timezone: "Europe/Berlin"     # IANA timezone name
    start: "22:00"                # 24h HH:MM
    end:   "07:00"                # wraps midnight correctly
```

Runtime control:

| Command                                       | Effect |
|-----------------------------------------------|---|
| `!quiet`                                      | Show status and window. |
| `!quiet on` / `!quiet off`                    | Enable/disable for this session. |
| `!quiet set 23:00 07:00 Europe/Berlin`        | Set window (session). |
| `!quiet reset`                                | Drop session overrides; use YAML again. |

### Chat allowlist (gating who Clawy talks to)

```yaml
chat:
  allowed_roles:
    - "Member"
```

Empty list = everyone can chat (default behavior). Non-empty = only members of
those roles get chat replies. Moderation applies to everyone regardless. Role
names are case-sensitive.

| Command                                       | Effect |
|-----------------------------------------------|---|
| `!chatroles`                                  | Show current list. |
| `!chatroles add <role>`                       | Add a role. |
| `!chatroles remove <role>`                    | Remove a role. |
| `!chatroles clear`                            | Empty the list (everyone chats). |
| `!chatroles reset`                            | Drop session override; use YAML. |

---

## 14. Memory and database

SQLite at `data/bot.db` (WAL mode, async-safe, created automatically).
Moderation memory and chat memory are in separate tables and are never mixed.

### Moderation tables

| Table         | Content |
|---------------|---|
| `users_seen`  | Display name, first/last seen, message count, server join timestamp, optional notes. |
| `mod_events`  | Append-only: every action (warn, timeout, kick, ban, delete) with reason and source. |
| `bot_actions` | Non-mod actions: moves, role grants, welcomes. |

### Chat tables

| Table        | Content |
|--------------|---|
| `chat_turns` | Per-user rolling conversation history. Pruned to `chat_keep_last_turns` (default 50). |
| `chat_notes` | Reserved for future long-term summaries; currently unused. |

The bot feeds the last `chat_context_turns` turns (default 8) into the LLM when
chatting, so Clawy remembers what was said earlier.

### Activity tables

| Table          | Content |
|----------------|---|
| `activity_log` | Every observed message: user_id, channel_id, guild_id, timestamp. Pruned to 35 days. |
| `role_grants`  | Tracks which role rules have already fired for which users. |

### Memory commands

| Command         | Effect |
|-----------------|---|
| `!recall @user` | Show the last 10 chat turns Clawy remembers with this user. |
| `!forget @user` | Wipe chat memory for this user (mod history is untouched). |
| `!whois @user`  | DB profile: first seen, last seen, message count, notes. |
| `!strikes @user`| Strike count and last 5 mod events. |

---

## 15. All admin commands

All commands require **Administrator** permission **or** matching `owner_id`.
Regular users get no response when they try — their `!command` message is
silently deleted.

**Where output goes:** transient confirmations ("Paused", usage hints, errors)
appear briefly in the channel where you typed and self-delete after a few
seconds. Informational output (`!diag`, `!whois`, `!strikes`, `!perms`,
listings) is routed to the configured `log_channel_id` so regular users don't
see admin diagnostics — with a brief "Sent to #log." breadcrumb where you ran
the command. If no log channel is set or Clawy can't write there, output falls
back to the source channel.

Run `!help` in Discord for a live grouped list, or `!help <command>` for the
docstring of any specific command.

### Bot control / kill switch

| Command                                                | Effect |
|--------------------------------------------------------|---|
| `!pause`                                               | Disable all autonomous actions. |
| `!resume`                                              | Re-enable autonomous actions. |
| `!sleep`                                               | Sleep indefinitely. |
| `!sleep 30m` / `!sleep 2h` / `!sleep 1h30m`            | Sleep for a duration, auto-wake. |
| `!wake`                                                | Wake immediately. |
| `!sleepstatus`                                         | Show sleep state and time-until-wake. |
| `!diag`                                                | Health check: Ollama, model, mode, persona, gating, DB. |

### Mode and persona

| Command                            | Effect |
|------------------------------------|---|
| `!mode`                            | Show current mode and options. |
| `!mode moderate_only`              | Switch mode (session). |
| `!mode chat_and_moderate`          | Switch mode (session). |
| `!mode chat_only`                  | Switch mode (session). |
| `!persona`                         | List all personas with descriptions and moods. |
| `!persona <key>`                   | Switch persona — e.g. `!persona nyx`. |
| `!persona reload`                  | Reload `personas.json` from disk. |
| `!mood`                            | Show active mood and available options. |
| `!mood <name>`                     | Switch mood — e.g. `!mood stern`. |

### Model and thinking

| Command                            | Effect |
|------------------------------------|---|
| `!model`                           | Show current Ollama model. |
| `!model <name>`                    | Switch model for the session — e.g. `!model qwen3:8b`. |
| `!think`                           | Show current thinking state. |
| `!think on` / `!think off`         | Toggle the model's reasoning trace. |
| `!think reset`                     | Drop session override; use YAML value. |

### Chat gating

Control *when* and *who* Clawy chats with. Moderation always runs regardless.

| Command                                          | Effect |
|--------------------------------------------------|---|
| `!quiet`                                         | Show quiet-hours status and window. |
| `!quiet on` / `!quiet off`                       | Enable/disable. |
| `!quiet set 23:00 07:00 Europe/Berlin`           | Set window (session). |
| `!quiet reset`                                   | Drop overrides; use YAML. |
| `!chatroles`                                     | Show chat role allowlist. |
| `!chatroles add <role>` / `!chatroles remove <role>` | Manage allowlist. |
| `!chatroles clear`                               | Empty allowlist (everyone chats). |
| `!chatroles reset`                               | Drop override; use YAML. |
| `!proactive`                                     | Show proactive-reply chance. |
| `!proactive 0.03`                                | Set to 3% per eligible message. |
| `!proactive off`                                 | Disable proactive replies. |
| `!proactive reset`                               | Drop override; use YAML. |
| `!jumpin`                                        | Make Clawy jump into the last 5 channel messages. |
| `!jumpin 10`                                     | Same, but the last N (capped at 20). |

### Manual moderation

| Command                                          | Effect |
|--------------------------------------------------|---|
| `!kick @user [reason]`                           | Kick a member. |
| `!ban @user [reason]`                            | Ban a member. |
| `!mute @user [duration] [reason]`                | Timeout (e.g. `!mute @x 30m spam`). |
| `!unmute @user`                                  | Remove a timeout. |

Duration formats accepted: `30s`, `30m`, `2h`, `1h30m`, `1h30m20s`.

### User info and memory

| Command          | Effect |
|------------------|---|
| `!whois @user`   | DB profile: first seen, last seen, message count. |
| `!strikes @user` | Strike count + last 5 moderation events. |
| `!recall @user`  | Last 10 chat memory turns. |
| `!forget @user`  | Wipe chat memory (moderation history untouched). |

### Message moving

| Command                              | Effect |
|--------------------------------------|---|
| `!moveto #channel`                   | Move replied message to channel. |
| `!moveto #channel N`                 | Move replied message + up to N more from same author. |
| `!movelast @user N #channel`         | Move last N messages from user in this channel. |

### Activity-based roles

| Command                          | Effect |
|----------------------------------|---|
| `!roles`                         | List loaded role rules. |
| `!roles reload`                  | Reload `role_rules.json` from disk. |
| `!roles check @user`             | Immediately evaluate rules for a user. |
| `!roles grants @user`            | Show which rules have fired for a user. |
| `!roles reset @user <rule_id>`   | Clear a grant so the rule can fire again. |

### Diagnostics and utilities

| Command                          | Effect |
|----------------------------------|---|
| `!help`                          | List all commands grouped by function. |
| `!help <command>`                | Show detailed usage for one command. |
| `!perms`                         | Show Clawy's permissions in the current channel + role hierarchy. |
| `!setlog #channel`               | Set log channel for the session. |

---

## 16. Full config reference

`config/config.yaml`:

```yaml
# ── Discord ──────────────────────────────────────────────────────────
guild_id: 0          # REQUIRED — your server ID
owner_id: 0          # REQUIRED — your Discord user ID. Bot never acts on you.
log_channel_id: 0    # Private admin/log channel. 0 = disabled.
command_prefix: "!"

# ── Bot mode ─────────────────────────────────────────────────────────
mode: "chat_and_moderate"
# Options: moderate_only | chat_and_moderate | chat_only

# ── Address matching ─────────────────────────────────────────────────
# If true, the bot also responds when called by the active persona's name
# (e.g. "Seraphael hello"). If false (default), only the bot's Discord
# display name + @mention work, avoiding confusion when personas change.
respond_to_persona_name: false

# ── Storage ──────────────────────────────────────────────────────────
database:
  path: "data/bot.db"          # SQLite file (WAL mode), auto-created
  chat_keep_last_turns: 50     # max stored chat turns per user
  chat_context_turns: 8        # how many past turns fed to the LLM per reply

# ── Protected roles (never punished autonomously) ────────────────────
protected_roles:
  - "Admin"
  - "Moderator"
  - "Owner"

# ── Ignored channels (read nothing, write nothing) ───────────────────
ignored_channels:
  - "staff-only"

# ── Ollama ───────────────────────────────────────────────────────────
ollama:
  model: "hermes3:3b"          # model identifier
  temperature: 0.85            # randomness; 0.6–0.9 is sane for chat
  num_ctx: 4096                # context window in tokens
  num_thread: 6                # CPU threads (match physical cores)
  f16_kv: false                # KV cache precision; false saves RAM
  num_predict: 320             # max tokens generated per response
  timeout_seconds: 60          # API request timeout
  think: false                 # internal reasoning trace; false = faster
  use_json_format: true        # forces valid JSON output

# ── Moderation ───────────────────────────────────────────────────────
moderation:
  blocklist_enabled: false                 # opt-in zero-tolerance words
  blocklist_file: "config/blocklist.json"

  proactive_reply_cooldown_seconds: 300    # min seconds between proactive replies in a channel
  proactive_reply_chance: 0.02             # 0.0 = off, 0.02 = 2% per eligible message

  spam_threshold: 6                        # messages in window that trigger spam warning
  spam_window_seconds: 10                  # window for above
  spam_strike_threshold: 3                 # total strikes (any kind) in strike_window_hours that escalate spam → timeout+delete
  spam_timeout_seconds: 600                # mute duration on escalation
  strike_window_hours: 24                  # rolling strike window
  default_timeout_seconds: 600             # default mute when LLM picks "timeout" with no duration

  # Mention rate limit (how often a user can @mention the bot)
  mention_max: 4                           # max mentions allowed in window
  mention_window_seconds: 30               # sliding window
  mention_reset_seconds: 120               # quiet time before strike resets
  mention_timeout_seconds: 300             # mute duration on second breach

# ── Autonomous timeout cap ───────────────────────────────────────────
# Even when the LLM picks "timeout", it is clamped to this maximum.
# Longer mutes require a human via !mute @user <duration>.
max_autonomous_timeout_seconds: 600

# ── Move command ─────────────────────────────────────────────────────
move:
  max_batch: 25                # safety cap on a single !moveto / !movelast
  post_notice: true            # post brief "moved" notice in source channel

# ── Allowed LLM actions ──────────────────────────────────────────────
# Kick and ban are intentionally absent — the LLM can never execute them.
allowed_actions:
  - reply
  - delete
  - warn
  - timeout
  - assign_role
  - remove_role
  - ignore

# ── Chat gating ──────────────────────────────────────────────────────
chat:
  # Empty = everyone can chat. Non-empty = only members of these roles.
  allowed_roles:
    - "Member"

  # Quiet hours — suppress chat + proactive replies during a window.
  # Moderation, prefilter, blocklist, rate-limiting, role engine still run.
  quiet_hours:
    enabled: false
    timezone: "Europe/Berlin"  # IANA name (zoneinfo)
    start: "22:00"             # HH:MM, 24h
    end:   "07:00"             # wraps midnight correctly
```

`.env`:

```
DISCORD_TOKEN=your-bot-token
OLLAMA_URL=http://localhost:11434
```

---

## 17. Recommended Ollama models

Pick the smallest model that holds character on your hardware. CPU-only setups
should stay at 3B–4B. GPU users can comfortably go to 7B–8B and beyond.

| Hardware                 | Suggested model       | Notes |
|--------------------------|-----------------------|---|
| CPU, ≤8 GB RAM           | `hermes3:3b`          | Default. Optimized for steerability and roleplay. |
| CPU, 16 GB RAM           | `qwen3:4b`            | More coherent on long contexts. |
| GPU, 8 GB VRAM           | `qwen3:8b`            | Best balance of personality and speed. |
| GPU, 12+ GB VRAM         | `hermes3:8b` / `qwen3:14b` | Strong roleplay, slower on CPU. |

If a model frequently breaks character or refuses persona prompts, it's almost
always the model's safety filter — try a different model. `!model <name>` swaps
for the session.

---

## 18. File layout

```
discord-bot/
├── install.bat / install.sh         run once to set up the environment
├── start.bat / start.sh             run every time to start the bot
├── main.py                          entry point (registers cogs)
├── requirements.txt
├── .env.example                     copy to .env and fill in your token
├── Dockerfile                       container image definition
├── docker-compose.yml               standard compose
├── docker-compose.truenas.yml       TrueNAS-friendly variant
├── DOCKER.md                        container deployment guide
├── QUICKSTART.md                    short install summary
│
├── config/
│   ├── config.yaml                  main configuration
│   ├── personas.json                personas and moods
│   ├── role_rules.json              activity-based role rules
│   └── blocklist.json.example       optional zero-tolerance word list
│
├── core/
│   ├── config.py                    loads config.yaml + .env
│   ├── persona.py                   reads/writes personas.json
│   ├── store.py                     SQLite layer (mod + chat + activity)
│   ├── ollama_client.py             async Ollama HTTP client
│   ├── prompts.py                   builds LLM system/user prompts
│   ├── prefilter.py                 fast rule-based pre-filter
│   ├── executor.py                  executes actions with guardrails
│   ├── tracking.py                  in-memory spam + mention rate limiters
│   └── gating.py                    quiet-hours + chat allowlist helpers
│
├── cogs/
│   ├── _common.py                   CleanCommandCog base, ack / reply helpers
│   ├── moderation.py                main on_message listener and chat router
│   ├── admin.py                     all !commands
│   ├── members.py                   welcome on member join
│   ├── move.py                      webhook-based message moving
│   ├── sleep.py                     !sleep / !wake with auto-wake timer
│   └── roles.py                     activity-based role assignment engine
│
└── data/
    └── bot.db                       SQLite database (auto-created)
```

---

## 19. Troubleshooting

**Bot is online but does not respond.**
Run `!diag` — is Ollama reachable? Confirm `guild_id` matches your server. In
the Developer Portal, check Message Content Intent and Server Members Intent
are both enabled.

**`I need Manage Webhooks in #channel`.**
Channel Settings → Permissions → find Clawy's role → enable Manage Webhooks.

**Roles not granted, or wrong role granted on restart.**
1. Make sure the role exists in Discord with an exact name match.
2. Make sure Clawy's role is positioned **above** the role she's granting.
3. Run `!roles grants @user` to see what the bot thinks has fired.
4. Use `!roles reset @user <rule_id>` to clear a stale grant record.
5. Run `!roles check @user` to immediately re-evaluate.

**`!mood` lists no moods.**
Your `personas.json` has a structural problem. Validate it:
```bash
python -c "import json; d=json.load(open('config/personas.json')); print(list(d['personas'][d['active_persona']]['moods'].keys()))"
```
If it errors or returns an empty list, fix missing commas/brackets, then run
`!persona reload`.

**Clawy refuses to stay in character ("I'm not interested...").**
The model's safety filter is overriding the persona prompt. Switch to a less
restrictive model with `!model <name>` (see section 17).

**Ollama is timing out.**
Increase `ollama.timeout_seconds`. On GPU setups, check `nvidia-smi` — if VRAM
is full, the model spills to RAM and slows dramatically. Use a smaller
quantization or smaller model.

**Strikes seem missing after restart.**
Strikes are persisted in `data/bot.db`. Run `!strikes @user` to confirm. If the
file doesn't exist, the database failed to initialize — check the bot's startup
logs for permission errors.

**Bot acts on protected users.**
Role names in `protected_roles` must match exactly as they appear in Discord,
including capitalization.

**Activity-based roles only count messages from after install.**
This is by design. Clawy only counts messages she has personally observed; she
does not back-scan channel history. Tenure, however, uses Discord's real
`joined_at` timestamp and works regardless of when the bot was installed.
