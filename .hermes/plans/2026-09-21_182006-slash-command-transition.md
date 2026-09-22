# Slash Command Transition Implementation Plan

> **For Hermes:** Implement smallest safe migration. Keep legacy handlers only while slash commands replace them.

**Goal:** Add typed Discord slash commands for usable moderator and administrator actions, then disable `!` commands by default.

**Architecture:** Reuse existing command callbacks through `commands.Context.from_interaction()`. Add interaction-safe permission checks and response helpers. Register a compact slash surface in `main.py`; no LLM or moderation executor changes. Use message context menus for reply-dependent commands later; they cannot be correct slash commands without a message target.

**Tech Stack:** Python 3.11, discord.py 2.3+, pytest.

---

## Current status

- [x] `discord.py>=2.3.2` supports application commands.
- [x] Existing commands use `commands.Context` and `CleanCommandCog` cleanup.
- [x] No application-command tree sync exists.
- [x] No test suite exists.
- [x] Implement reusable interaction authorization and slash dispatch.
- [x] Register, sync, and test slash command surface.
- [x] Disable `!` prefix by default and document command scope.

**Implemented:** `/pause`, `/resume`, `/mode`, `/persona`, `/mood`, `/model`, `/think`, `/diagnostics`, `/strikes`, `/whois`, `/kick`, `/ban`, `/mute`, `/unmute`.

**Verified:** `uv run --with pytest --with-requirements requirements.txt pytest -q` returns 4 passed. `uv run --with-requirements requirements.txt python -m compileall -q .` exits 0.

## Task 1: Add interaction authorization and dispatch helpers

**Files:**
- Modify: `cogs/_common.py`
- Test: `tests/test_slash_commands.py`

1. Write tests for owner/admin/mod checks from interaction-like objects.
2. Add helpers accepting `discord.Interaction`.
3. Add slash dispatcher that creates `commands.Context` and calls existing command callbacks without command-message deletion.

## Task 2: Register typed high-value slash commands

**Files:**
- Modify: `main.py`
- Test: `tests/test_slash_commands.py`

1. Add `/pause`, `/resume`, `/mode`, `/persona`, `/mood`, `/model`, `/think`, `/diagnostics` for admin users.
2. Add `/strikes`, `/whois`, `/purge`, `/purge-user`, `/move-last` for moderators.
3. Use native member/channel/integer input types and explicit range checks.
4. Run `bot.tree.sync(guild=...)` during startup when `guild_id` is configured; otherwise global sync.

## Task 3: Retire prefix command entry point

**Files:**
- Modify: `config/config.yaml`, `README.md`
- Test: `tests/test_slash_commands.py`

1. Make `command_prefix` empty by default so `!` commands do not execute.
2. Document slash commands, required `applications.commands` invite scope, and restart/sync behavior.
3. Document excluded reply-dependent commands: `moveto` and `purgethis` need message context-menu implementation before exposure.

## Validation

Run `pytest -q`. Compile Python source with `python -m compileall -q .`. Verify command registration using a local `commands.Bot` and command-tree inspection. Do not connect or sync against Discord during tests.
