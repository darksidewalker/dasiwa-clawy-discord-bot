#!/usr/bin/env bash
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────
RED='\033[0;31m'
YEL='\033[1;33m'
NC='\033[0m'

err() { echo -e " ${RED}[ERROR]${NC} $*" >&2; exit 1; }
warn() { echo -e " ${YEL}[WARN]${NC} $*" >&2; }

# ── Sanity checks ────────────────────────────────────────────────────
[ -f ".venv/bin/python" ] || err "Virtual environment not found. Run ./install.sh first."
[ -f ".env" ] || err ".env file not found. Copy .env.example to .env and fill in your DISCORD_TOKEN."
[ -f "config/config.yaml" ] || err "config/config.yaml not found."

# Check that .env actually has a token
if ! grep -q "^DISCORD_TOKEN=.\+" .env 2>/dev/null; then
    warn ".env exists but DISCORD_TOKEN appears to be empty."
    echo "      Open .env and paste your Discord bot token."
    exit 1
fi

# ── Start ─────────────────────────────────────────────────────────────
echo " Starting Clawy..."
echo " Press Ctrl+C to stop."
echo ""

# Run Python from the venv directly — no activation needed
exec .venv/bin/python main.py
