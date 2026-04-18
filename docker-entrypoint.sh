#!/bin/sh
# docker-entrypoint.sh
# ─────────────────────────────────────────────────────────────────────
# Boot order:
#   1. Seed missing config files from baked-in defaults (if mount is writable)
#   2. Load variables from /app/.env (if present)
#   3. Apply ENV → config.yaml overrides (GUILD_ID, OWNER_ID, etc.)
#   4. If DISCORD_TOKEN missing → idle-loop and poll for it instead of exiting
#   5. Exec the bot
#
# Design notes:
#   - Container stays alive on missing token so TrueNAS / Portainer / etc.
#     don't crash-loop. User can add the env var + restart, or drop a .env
#     in place without touching the container definition.
#   - Env overrides are written into config.yaml at boot. This means
#     core/config.py doesn't need to know about Docker — it just reads its
#     normal file. Local non-Docker setups are unaffected.
# ─────────────────────────────────────────────────────────────────────

# NOTE: `set -e` is intentionally NOT used globally. We handle errors
# ourselves so a read-only config mount or malformed YAML doesn't kill
# the container before we can print a useful message.

# ── Colours ───────────────────────────────────────────────────────────
if [ -t 1 ]; then
    RED='\033[0;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; NC='\033[0m'
else
    RED=''; YEL=''; GRN=''; NC=''
fi

info()  { printf '%b[clawy]%b %s\n' "$GRN" "$NC" "$*"; }
warn()  { printf '%b[clawy]%b %s\n' "$YEL" "$NC" "$*"; }
error() { printf '%b[clawy]%b %s\n' "$RED" "$NC" "$*" >&2; }

CONFIG_DIR="/app/config"
DEFAULTS_DIR="/app/defaults/config"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
ENV_FILE="/app/.env"

# ── Step 1: seed missing config files ─────────────────────────────────
info "Checking config directory..."

CONFIG_WRITABLE=1
if ! [ -w "${CONFIG_DIR}" ]; then
    CONFIG_WRITABLE=0
    warn "  ${CONFIG_DIR} is NOT writable (mounted read-only?)"
    warn "  Defaults cannot be seeded. The bot will only start if you've"
    warn "  pre-populated the volume with config files."
fi

for f in config.yaml personas.json role_rules.json; do
    if [ ! -f "${CONFIG_DIR}/${f}" ]; then
        if [ "${CONFIG_WRITABLE}" = "1" ]; then
            if cp "${DEFAULTS_DIR}/${f}" "${CONFIG_DIR}/${f}" 2>/dev/null; then
                info "  ${f} seeded from defaults"
            else
                error "  ${f} missing and copy failed"
            fi
        else
            error "  ${f} missing and volume is read-only — cannot seed"
        fi
    else
        info "  ${f} present"
    fi
done

# ── Step 2: load .env file if present ─────────────────────────────────
load_env_file() {
    [ -f "${ENV_FILE}" ] || return 0
    # Parse KEY=VALUE lines manually to avoid shell-interpretation issues
    # with tokens containing special characters.
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|\#*) continue ;;
        esac
        # Only accept lines that look like KEY=VALUE
        case "$line" in
            *=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        val="${line#*=}"
        # Strip surrounding quotes from value if present
        val="${val%\"}"; val="${val#\"}"
        val="${val%\'}"; val="${val#\'}"
        # Validate key: letters, digits, underscores only
        case "$key" in
            *[!A-Za-z0-9_]*|'') continue ;;
        esac
        export "$key=$val"
    done < "${ENV_FILE}"
}

if [ -f "${ENV_FILE}" ]; then
    info ".env found at ${ENV_FILE} — loading"
    load_env_file
fi

# ── Step 3: apply env overrides to config.yaml ────────────────────────
# Any of these env vars, if set, are written into config.yaml before boot.
# This lets TrueNAS users configure everything via the compose env section
# without ever editing a YAML file.
#
# Supported overrides:
#   GUILD_ID, OWNER_ID, LOG_CHANNEL_ID  (integers)
#   BOT_MODE                            (string: moderate_only | chat_and_moderate | chat_only)
#   OLLAMA_MODEL                        (string, nested under ollama.model)
#
# NOTE: OLLAMA_URL is read by core/config.py directly from the process env —
# it is not written into config.yaml. Anything exported in step 2 above is
# inherited by the Python process, so no yaml write is needed.
#
# If config.yaml doesn't exist at this point, we skip silently — Python will
# error out clearly on its own.
apply_env_overrides() {
    [ -f "${CONFIG_FILE}" ] || return 0
    if ! [ -w "${CONFIG_FILE}" ]; then
        warn "config.yaml is read-only — env overrides cannot be applied"
        warn "Python will use whatever values are in the file as-is."
        return 0
    fi

    python3 - << 'PYEOF'
import os
import sys

try:
    import yaml
except ImportError:
    print("[clawy] PyYAML not available — skipping env overrides", file=sys.stderr)
    sys.exit(0)

CONFIG = "/app/config/config.yaml"

try:
    with open(CONFIG) as f:
        cfg = yaml.safe_load(f) or {}
except Exception as e:
    print(f"[clawy] Could not parse config.yaml ({e}) — skipping overrides", file=sys.stderr)
    sys.exit(0)

changed = []

def set_int(key, env_name):
    v = os.environ.get(env_name)
    if v is None or v == "":
        return
    try:
        n = int(v)
    except ValueError:
        print(f"[clawy] {env_name}={v!r} is not an integer — ignored")
        return
    if cfg.get(key) != n:
        cfg[key] = n
        changed.append(f"{key}={n}")

def set_str(key, env_name):
    v = os.environ.get(env_name)
    if v is None or v == "":
        return
    if cfg.get(key) != v:
        cfg[key] = v
        changed.append(f"{key}={v}")

def set_nested(parent, child, env_name):
    v = os.environ.get(env_name)
    if v is None or v == "":
        return
    if not isinstance(cfg.get(parent), dict):
        cfg[parent] = {}
    if cfg[parent].get(child) != v:
        cfg[parent][child] = v
        changed.append(f"{parent}.{child}={v}")

set_int("guild_id", "GUILD_ID")
set_int("owner_id", "OWNER_ID")
set_int("log_channel_id", "LOG_CHANNEL_ID")
set_str("mode", "BOT_MODE")
set_nested("ollama", "model", "OLLAMA_MODEL")

if changed:
    try:
        with open(CONFIG, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)
        print(f"[clawy] config.yaml updated from env: {', '.join(changed)}")
    except Exception as e:
        print(f"[clawy] Failed to write config.yaml: {e}", file=sys.stderr)
        sys.exit(0)
else:
    print("[clawy] No env overrides applied (config.yaml left as-is)")
PYEOF
}

apply_env_overrides

# ── Step 4: wait for DISCORD_TOKEN (non-blocking) ─────────────────────
# A token is "missing" if unset, empty, or still a known placeholder.
is_placeholder_token() {
    [ -z "$1" ] && return 0
    case "$1" in
        paste-*|your-*|YOUR_*|your-discord-bot-token-here) return 0 ;;
    esac
    return 1
}

if is_placeholder_token "${DISCORD_TOKEN}"; then
    error "════════════════════════════════════════════════════════════"
    error "  DISCORD_TOKEN is not set."
    error ""
    error "  The container will stay alive and poll for the token every"
    error "  10 seconds. Fix this by ONE of:"
    error ""
    error "    A) Set DISCORD_TOKEN in your compose/env and restart:"
    error "       environment:"
    error "         - DISCORD_TOKEN=your-actual-token"
    error ""
    error "    B) Drop a .env file onto the mounted volume at /app/.env"
    error "       (no restart needed — it is picked up automatically)"
    error ""
    error "  Get a token at https://discord.com/developers/applications"
    error "════════════════════════════════════════════════════════════"

    while is_placeholder_token "${DISCORD_TOKEN}"; do
        sleep 10
        # Refresh from .env in case the user dropped one in mid-flight.
        if [ -f "${ENV_FILE}" ]; then
            load_env_file
        fi
    done

    info "DISCORD_TOKEN detected — continuing startup"
    # Re-apply overrides in case the new .env carried more values
    # (GUILD_ID etc. dropped in alongside the token).
    apply_env_overrides
fi

info "DISCORD_TOKEN is set"

# ── Step 5: final sanity check on config.yaml ─────────────────────────
if [ -f "${CONFIG_FILE}" ]; then
    python3 - << 'PYEOF'
import yaml, sys
try:
    with open("/app/config/config.yaml") as f:
        cfg = yaml.safe_load(f) or {}
except Exception as e:
    print(f"[clawy] config.yaml is malformed: {e}")
    sys.exit(0)  # Don't block — let Python handle it with a better message

warns = []
if not cfg.get("guild_id"):
    warns.append("guild_id is 0 — set GUILD_ID env var or edit config.yaml")
if not cfg.get("owner_id"):
    warns.append("owner_id is 0 — set OWNER_ID env var or edit config.yaml")

if warns:
    for w in warns:
        print(f"[clawy] ⚠  {w}")
    print("[clawy] Bot will start but moderation on your server won't work correctly.")
else:
    print("[clawy] config.yaml looks good")
PYEOF
else
    error "config.yaml is missing at ${CONFIG_FILE} — bot will fail to start"
    error "If your volume is read-only, pre-populate it before mounting."
fi

# ── Step 6: launch bot ────────────────────────────────────────────────
info "Starting Clawy..."
exec python3 main.py
