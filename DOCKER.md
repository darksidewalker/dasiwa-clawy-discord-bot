# Running Clawy in Docker

Two deployment paths are supported:

1. **Local Docker / Docker Desktop** — build the image from source, run via
   `docker compose up -d`.
2. **TrueNAS SCALE** — pull a pre-built image, deploy via the Apps UI.

Both paths use the same container. They differ only in how the image gets to
the host and how you pass configuration in.

---

## Table of contents

1. [How configuration works in Docker](#1-how-configuration-works-in-docker)
2. [Local Docker (Linux / Mac / WSL)](#2-local-docker-linux--mac--wsl)
3. [TrueNAS SCALE](#3-truenas-scale)
4. [Troubleshooting Docker](#4-troubleshooting-docker)

---

## 1. How configuration works in Docker

Clawy reads config from three places, in **increasing priority**:

1. `config/config.yaml` inside the container (seeded with sane defaults on first start)
2. `.env` file mounted at `/app/.env` (optional)
3. Environment variables set in `docker-compose.yml` / the Apps UI

Higher layers win. So you can:

- Edit `config.yaml` on the host (via a mounted volume) — classic approach.
- Drop a `.env` on the mounted volume — token and overrides in one file.
- Set everything via the compose `environment:` block — no file editing at all.

**Supported env-var overrides:**

| Env var | Effect |
|---|---|
| `DISCORD_TOKEN` | Bot token (required) |
| `GUILD_ID` | Overrides `guild_id` in config.yaml |
| `OWNER_ID` | Overrides `owner_id` in config.yaml |
| `LOG_CHANNEL_ID` | Overrides `log_channel_id` in config.yaml |
| `BOT_MODE` | Overrides `mode` (`moderate_only` / `chat_and_moderate` / `chat_only`) |
| `OLLAMA_URL` | Tells the bot where Ollama lives |
| `OLLAMA_MODEL` | Overrides `ollama.model` in config.yaml |

The entrypoint script merges these into `config.yaml` at boot. Values you
don't set stay at whatever's in the file.

### First-start behaviour

- **Missing config files?** The container copies baked-in defaults into the
  mounted config volume automatically. No pre-seeding needed.
- **Missing `DISCORD_TOKEN`?** The container **does not crash**. It logs a
  clear message and polls every 10 seconds for the token to appear (via env
  var or a `.env` dropped onto the volume). Once it appears, boot resumes —
  no restart required in the .env case.
- **Read-only config mount?** The container detects this, logs a warning,
  and skips seeding. You must pre-populate the volume yourself.

---

## 2. Local Docker (Linux / Mac / WSL)

### Prerequisites

- Docker Engine or Docker Desktop
- Ollama running on the **host** (not inside Docker — bot connects to it over HTTP)

### Setup

From the project root:

```bash
# 1. Copy the env template and fill in your token
cp .env.example .env
$EDITOR .env

# 2. Optionally edit config.yaml (or leave it and set GUILD_ID/OWNER_ID in .env)
$EDITOR config/config.yaml

# 3. Build and start
docker compose up -d --build

# 4. Watch logs
docker compose logs -f clawy
```

### Talking to host Ollama from the container

Set `OLLAMA_URL` in `.env` or `docker-compose.yml` to one of:

| Host OS | Value |
|---|---|
| Docker Desktop (Mac/Windows) | `http://host.docker.internal:11434` |
| Linux native Docker | `http://172.17.0.1:11434` (docker0 bridge IP) |
| Any — always works | `http://<your-lan-ip>:11434` |

### Updating the bot

```bash
git pull
docker compose up -d --build
```

Your `data/` volume survives — SQLite database, strikes, chat memory etc. are
preserved across rebuilds.

---

## 3. TrueNAS SCALE

### Prerequisites

- A Docker registry account (Docker Hub, GHCR, or a local registry)
- A Linux or Mac machine to build and push the image from (TrueNAS can't
  build locally)
- Ollama running somewhere reachable on your LAN

### Setup (one-time)

**Step 1 — build and push the image** (from your build machine, not TrueNAS):

```bash
docker build -t yourusername/clawy-bot:latest .
docker push yourusername/clawy-bot:latest
```

**Step 2 — create TrueNAS datasets** (UI → Storage → your pool → Add Dataset):

```
/mnt/<pool>/clawy/config     ← leave empty; container seeds defaults
/mnt/<pool>/clawy/data       ← leave empty; SQLite lives here
```

Make sure both are writable by UID 568. The TrueNAS UI handles this
automatically when you create datasets via the web UI.

**Step 3 — deploy via Apps → Custom App**:

Paste the contents of `docker-compose.truenas.yml`, then **fill in the
placeholders**:

- `image:` — change to your pushed image name
- `DISCORD_TOKEN` — your bot token
- `GUILD_ID` — your Discord server ID
- `OWNER_ID` — your user ID
- `OLLAMA_URL` — your TrueNAS host's LAN IP + `:11434`
  (**not** `localhost` — containers can't reach the host via localhost)
- `/mnt/<pool>` paths — your actual pool name

Deploy. On first start, the container seeds its config files into the config
dataset and writes your env-var overrides into `config.yaml`.

### If you forgot the token

No problem — the container stays alive with a clear error in the logs:

```
[clawy] DISCORD_TOKEN is not set.
[clawy] The container will stay alive and poll for the token every
[clawy] 10 seconds. ...
```

Either fix the env var in the Apps UI and restart, or SSH in and drop a
`.env` file into `/mnt/<pool>/clawy/config/` — the bot picks it up within
10 seconds and continues starting up.

### Updating the bot

Rebuild and push on your build machine, then in TrueNAS Apps → your app →
**Pull Image** → **Restart**.

---

## 4. Troubleshooting Docker

**Container starts but says `DISCORD_TOKEN is not set`**
The token env var is empty or still a placeholder (`your-token-here`,
`paste-...`, etc.). Set it and restart the container, or drop a proper
`.env` onto the config volume.

**Container logs show `config.yaml is read-only — env overrides cannot be applied`**
Your config volume is mounted read-only. Either remove the `:ro` flag from
the volume declaration, or pre-populate the volume with the exact
`config.yaml` you want to use.

**Bot can't reach Ollama** (`Ollama at ... is NOT reachable`)
- You used `localhost` — change to the host LAN IP or `host.docker.internal`.
- Ollama is bound only to `127.0.0.1` on the host. Start it with
  `OLLAMA_HOST=0.0.0.0 ollama serve` (or set that env var in its systemd unit)
  so it accepts non-localhost connections.
- A firewall is blocking port 11434 between the container and host.

**Config changes don't take effect after editing `config.yaml`**
- Compose env vars override the file. If you set `GUILD_ID` in
  `docker-compose.yml`, editing `guild_id` in `config.yaml` has no effect —
  the entrypoint overwrites it on every boot.
- To make `config.yaml` authoritative, remove the matching env var from your
  compose `environment:` block and restart.

**Volume permission errors on TrueNAS**
Ensure the dataset is owned by UID 568 (TrueNAS apps user). If you created
the dataset outside the UI, fix ownership:

```
chown -R 568:568 /mnt/<pool>/clawy
```

**I want to see the live `config.yaml` the bot is using**

```bash
docker exec clawy cat /app/config/config.yaml
```

This shows the file after env overrides have been applied — useful for
confirming your compose values landed correctly.
