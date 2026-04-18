@echo off
setlocal enabledelayedexpansion
title Clawy Bot — Installer

echo.
echo  =============================================
echo   Clawy Discord Bot — Installer
echo  =============================================
echo.

:: ── Check uv ─────────────────────────────────────────────────────────
where uv >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] 'uv' was not found on your PATH.
    echo.
    echo  Install it from: https://docs.astral.sh/uv/getting-started/installation/
    echo  Quick install ^(PowerShell^):
    echo    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('uv --version 2^>^&1') do set UV_VER=%%v
echo  [OK] uv found: %UV_VER%

:: ── Check Python ─────────────────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Install Python 3.11+ from https://python.org
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo  [OK] Python found: %PY_VER%

:: ── Check Ollama ─────────────────────────────────────────────────────
where ollama >nul 2>&1
if errorlevel 1 (
    echo  [WARN] 'ollama' not found on PATH.
    echo         Download from: https://ollama.com
    echo         The bot will still install but won't work without Ollama running.
    echo.
) else (
    for /f "tokens=*" %%v in ('ollama --version 2^>^&1') do set OL_VER=%%v
    echo  [OK] Ollama found: %OL_VER%
)

:: ── Create virtual environment ────────────────────────────────────────
echo.
echo  Creating virtual environment with uv...
uv venv .venv --python 3.11
if errorlevel 1 (
    echo  [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)
echo  [OK] Virtual environment created at .venv\

:: ── Install dependencies ─────────────────────────────────────────────
echo.
echo  Installing dependencies from requirements.txt...
uv pip install -r requirements.txt --python .venv\Scripts\python.exe
if errorlevel 1 (
    echo  [ERROR] Dependency installation failed.
    pause
    exit /b 1
)
echo  [OK] All dependencies installed.

:: ── Create data directory ────────────────────────────────────────────
if not exist "data\" (
    mkdir data
    echo  [OK] Created data\ directory for the database.
)

:: ── Check .env ───────────────────────────────────────────────────────
echo.
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo  [OK] Created .env from .env.example
    )
    echo  [!!] IMPORTANT: Open .env and paste your Discord bot token.
) else (
    echo  [OK] .env already exists.
)

:: ── Done ─────────────────────────────────────────────────────────────
echo.
echo  =============================================
echo   Installation complete!
echo  =============================================
echo.
echo  Next steps:
echo    1. Edit .env          — paste your DISCORD_TOKEN
echo    2. Edit config\config.yaml — set guild_id, owner_id, log_channel_id
echo    3. Pull an Ollama model:
echo         ollama pull qwen3:8b
echo    4. Start the bot:
echo         start.bat
echo.
pause
