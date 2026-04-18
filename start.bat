@echo off
setlocal
title Clawy Bot

:: ── Sanity checks ────────────────────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo  [ERROR] Virtual environment not found.
    echo          Run install.bat first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo  [ERROR] .env file not found.
    echo          Copy .env.example to .env and fill in your DISCORD_TOKEN.
    pause
    exit /b 1
)

if not exist "config\config.yaml" (
    echo  [ERROR] config\config.yaml not found.
    pause
    exit /b 1
)

:: Check that .env actually has a token (basic check)
findstr /R /C:"^DISCORD_TOKEN=..*" .env >nul 2>&1
if errorlevel 1 (
    echo  [WARN] .env exists but DISCORD_TOKEN appears to be empty.
    echo         Open .env and paste your Discord bot token.
    pause
    exit /b 1
)

:: ── Start ─────────────────────────────────────────────────────────────
echo  Starting Clawy...
echo  Press Ctrl+C to stop.
echo.
.venv\Scripts\python.exe main.py

:: If the bot exits with an error, keep the window open
if errorlevel 1 (
    echo.
    echo  [!!] Bot exited with an error. See output above.
    pause
)
