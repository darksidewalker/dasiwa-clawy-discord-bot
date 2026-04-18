#!/bin/sh
# Clawy bot entrypoint
# On first start: copies default configs into the mounted volume.
# On subsequent starts: leaves existing configs untouched.
set -e

GRN='\033[0;32m'
YEL='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { printf "${GRN}[clawy]${NC} %s\n" "$*"; }
warn()  { printf "${YEL}[clawy]${NC} %s\n" "$*"; }
error() { printf "${RED}[clawy]${NC} %s\n" "$*" >&2; }

# ── Seed config on first run ──────────────────────────────────────────
# Copies each default file only if it does not already exist in the volume.
# Existing files are never overwritten — your edits are always preserved.
info "Checking config..."
for f in config.yaml personas.json role_rules.json; do
    if [ ! -f "/app/config/${f}" ]; then
        cp "/app/defaults/config/${f}" "/app/config/${f}"
        warn "  Created default: config/${f}  ← edit this file"
    else
        info "  Found: config/${f}"
    fi
done

# ── Load .env if mounted ──────────────────────────────────────────────
if [ -f "/app/.env" ]; then
    info "Loading /app/.env"
    set -a
    # shellcheck disable=SC1091
    . /app/.env
    set +a
fi

# ── Validate token ────────────────────────────────────────────────────
if [ -z "${DISCORD_TOKEN}" ]; then
    error ""
    error "  DISCORD_TOKEN is not set. Add it to your compose:"
    error "    environment:"
    error "      - DISCORD_TOKEN=your-token-here"
    error "  Or mount a .env file to /app/.env"
    error ""
    exit 1
fi
info "DISCORD_TOKEN OK"

# ── Warn about unconfigured placeholders (soft warning, still starts) ─
python3 - << 'PYEOF'
import yaml, sys
try:
    with open("/app/config/config.yaml") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("guild_id") or int(cfg.get("guild_id", 0)) == 0:
        print("\033[1;33m[clawy]\033[0m  ⚠  config/config.yaml: guild_id is still 0")
    if not cfg.get("owner_id") or int(cfg.get("owner_id", 0)) == 0:
        print("\033[1;33m[clawy]\033[0m  ⚠  config/config.yaml: owner_id is still 0")
except Exception as e:
    print(f"\033[0;31m[clawy]\033[0m Cannot read config.yaml: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

# ── Start ─────────────────────────────────────────────────────────────
info "Starting Clawy..."
exec python3 main.py
