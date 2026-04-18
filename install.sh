#!/usr/bin/env bash
set -euo pipefail

# ── Self-check: ensure this script is executable ────────────────────
if [ ! -x "$0" ]; then
    echo " [!!] This script is not executable. Fixing..."
    chmod +x "$0"
    echo "      Please run it again: ./install.sh"
    exit 0
fi

# ── Colours ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GRN='\033[0;32m'
YEL='\033[1;33m'
NC='\033[0m'

ok()   { echo -e " ${GRN}[OK]${NC}   $*"; }
warn() { echo -e " ${YEL}[WARN]${NC} $*"; }
err()  { echo -e " ${RED}[ERR]${NC}  $*"; exit 1; }

echo ""
echo " ============================================="
echo "  Clawy Discord Bot — Installer"
echo " ============================================="
echo ""

# ── Check uv ─────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    err "'uv' not found. Install it from https://docs.astral.sh/uv/getting-started/installation/
       Quick install: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi
ok "uv found: $(uv --version)"

# ── Check Python ─────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    err "Python 3 not found. Install Python 3.11+ from https://python.org"
fi
PY_VER=$(python3 --version 2>&1)
ok "Python found: $PY_VER"

# ── Check Ollama ─────────────────────────────────────────────────────
if ! command -v ollama &>/dev/null; then
    warn "'ollama' not found. Download from https://ollama.com
         The bot will still install but won't work without Ollama running."
else
    ok "Ollama found: $(ollama --version 2>/dev/null || echo 'unknown version')"
fi

# ── Create virtual environment ────────────────────────────────────────
echo ""
echo " Creating virtual environment with uv..."
uv venv .venv --python 3.11 || err "Failed to create virtual environment."
ok "Virtual environment created at .venv/"

# ── Install dependencies ─────────────────────────────────────────────
echo ""
echo " Installing dependencies from requirements.txt..."
uv pip install -r requirements.txt --python .venv/bin/python \
    || err "Dependency installation failed."
ok "All dependencies installed."

# ── Create data directory ────────────────────────────────────────────
if [ ! -d "data" ]; then
    mkdir -p data
    ok "Created data/ directory for the database."
fi

# ── Check .env ───────────────────────────────────────────────────────
echo ""
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        ok "Created .env from .env.example"
    fi
    warn "IMPORTANT: Open .env and paste your DISCORD_TOKEN."
else
    ok ".env already exists."
fi

# ── Make scripts executable ──────────────────────────────────────────
chmod +x start.sh 2>/dev/null || true
chmod +x install.sh 2>/dev/null || true  # in case user extracted without -x
ok "Scripts are now executable."

# ── Done ─────────────────────────────────────────────────────────────
echo ""
echo " ============================================="
echo "  Installation complete!"
echo " ============================================="
echo ""
echo " Next steps:"
echo "   1. Edit .env                  — paste your DISCORD_TOKEN"
echo "   2. Edit config/config.yaml    — set guild_id, owner_id, log_channel_id"
echo "   3. Pull an Ollama model:"
echo "        ollama pull qwen3:8b"
echo "   4. Start the bot:"
echo "        ./start.sh"
echo ""
