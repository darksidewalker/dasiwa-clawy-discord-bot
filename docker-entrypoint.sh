#!/bin/sh
set -e

# ── Colours ───────────────────────────────────────────────────────────
RED='\033[0;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'

info()  { echo "${GRN}[clawy]${NC} $*"; }
warn()  { echo "${YEL}[clawy]${NC} $*"; }
error() { echo "${RED}[clawy]${NC} $*" >&2; }

# ── First-run: seed missing config files from baked-in defaults ───────
# If /app/config is empty (e.g. freshly mounted empty volume or no volume),
# copy the default config files in so the bot can start.
info "Checking config directory..."

for f in config.yaml personas.json role_rules.json; do
    if [ ! -f "/app/config/${f}" ]; then
        warn "  ${f} not found — copying default from image"
        cp "/app/defaults/config/${f}" "/app/config/${f}"
    else
        info "  ${f} found"
    fi
done

# ── Validate DISCORD_TOKEN ────────────────────────────────────────────
# Check .env file first (mounted volume approach)
if [ -f "/app/.env" ]; then
    info ".env found — loading"
    # shellcheck disable=SC2046
    export $(grep -v '^#' /app/.env | grep -v '^$' | xargs) 2>/dev/null || true
fi

# Check token
if [ -z "${DISCORD_TOKEN}" ] || echo "${DISCORD_TOKEN}" | grep -q "^paste-\|^your-"; then
    error ""
    error "══════════════════════════════════════════════════"
    error "  DISCORD_TOKEN is not set."
    error ""
    error "  Fix ONE of these:"
    error "  1. Mount a .env file to /app/.env"
    error "     (copy .env.example → .env, fill in token)"
    error ""
    error "  2. Set the DISCORD_TOKEN environment variable"
    error "     directly in your docker-compose.yml:"
    error "     environment:"
    error "       - DISCORD_TOKEN=your-token-here"
    error "══════════════════════════════════════════════════"
    error ""
    exit 1
fi
info "DISCORD_TOKEN is set"

# ── Validate config.yaml has required fields ──────────────────────────
if python3 - << 'PYEOF'
import yaml, sys
with open("/app/config/config.yaml") as f:
    cfg = yaml.safe_load(f) or {}
errors = []
if not cfg.get("guild_id") or cfg.get("guild_id") == 0:
    errors.append("guild_id is 0 — set it to your Discord server ID")
if not cfg.get("owner_id") or cfg.get("owner_id") == 0:
    errors.append("owner_id is 0 — set it to your Discord user ID")
if errors:
    print("\n".join(f"  ⚠  {e}" for e in errors))
    sys.exit(1)
PYEOF
then
    info "config.yaml looks good"
else
    warn ""
    warn "config/config.yaml has placeholder values."
    warn "The bot will start but moderation won't work correctly."
    warn "Edit guild_id and owner_id in config/config.yaml."
    warn ""
    # Don't exit — let it start anyway so user can fix config via volume
fi

# ── Start the bot ─────────────────────────────────────────────────────
info "Starting Clawy..."
exec python3 main.py
