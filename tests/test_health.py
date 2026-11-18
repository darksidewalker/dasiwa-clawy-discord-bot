"""Liveness heartbeat (core/health.py) — the container healthcheck contract.

The compose `healthcheck:` runs `python3 -m core.health`, so these tests pin the
three states the probe must distinguish: fresh stamp (healthy), stale stamp
(process alive but stuck → unhealthy), no stamp at all (unhealthy). The last two
tests cover main.py's wiring, including that the Ollama reachability probe stays
inside `on_ready` (an event inserted in the middle of that handler silently
swallowed it once).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import health
from core.config import CFG
import main

ROOT = Path(__file__).resolve().parents[1]


def test_fresh_stamp_is_healthy(tmp_path):
    path = tmp_path / "health.json"
    assert health.write("gateway_connected", str(path)) is True
    assert health.age(str(path)) < 1

    healthy, detail = health.check(path=str(path))
    assert healthy
    assert "gateway_connected" in detail


def test_missing_stamp_is_unhealthy(tmp_path):
    path = tmp_path / "missing.json"
    assert health.age(str(path)) == float("inf")

    healthy, detail = health.check(path=str(path))
    assert not healthy
    assert "no heartbeat" in detail


def test_stale_stamp_is_unhealthy(tmp_path):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"ts": time.time() - 120, "pid": 1, "state": "starting"}))

    healthy, detail = health.check(max_age=90, path=str(path))
    assert not healthy
    assert "starting" in detail


def test_stamp_is_written_atomically(tmp_path):
    path = tmp_path / "health.json"
    health.write("running", str(path))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["state"] == "running"
    assert data["pid"] == os.getpid()
    # The tmp half-write is renamed away, never left beside the real stamp.
    assert not Path(str(path) + ".tmp").exists()


def test_unwritable_path_never_raises(tmp_path):
    # A read-only data mount must never take the bot down.
    assert health.write("running", str(tmp_path / "absent" / "health.json")) is False


def test_unwritable_configured_path_falls_back(tmp_path, monkeypatch):
    """If the data volume cannot be stamped, the heartbeat must still land."""
    blocked = tmp_path / "blocked"      # a file where a directory must be
    blocked.write_text("not a directory", encoding="utf-8")
    fallback = tmp_path / "fallback.json"

    monkeypatch.setattr(health, "HEALTH_FILE", str(blocked / "health.json"))
    monkeypatch.setattr(health, "FALLBACK_HEALTH_FILE", str(fallback))
    health.reset()
    try:
        assert health.health_path() == str(fallback)
        assert health.write("gateway_connected") is True

        healthy, detail = health.check()
        assert healthy
        assert "fallback" in detail and "gateway_connected" in detail
    finally:
        health.reset()


def test_resolution_is_cached_and_resettable(tmp_path, monkeypatch):
    first = tmp_path / "a.json"
    monkeypatch.setattr(health, "HEALTH_FILE", str(first))
    monkeypatch.setattr(health, "FALLBACK_HEALTH_FILE", str(tmp_path / "b.json"))
    health.reset()
    assert health.health_path() == str(first)

    # A different HEALTH_FILE only takes effect after reset().
    second = tmp_path / "c.json"
    monkeypatch.setattr(health, "HEALTH_FILE", str(second))
    assert health.health_path() == str(first)
    health.reset()
    assert health.health_path() == str(second)


def test_beat_forever_tracks_state(tmp_path):
    path = tmp_path / "health.json"
    state = {"state": "starting"}

    async def run():
        task = asyncio.create_task(health.beat_forever(state, str(path), interval=0.01))
        try:
            await asyncio.sleep(0.05)
            state["state"] = "gateway_connected"
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "gateway_connected"


def test_on_ready_marks_gateway_connected_and_keeps_ollama_probe(monkeypatch):
    """main.on_ready must stamp the heartbeat AND still probe Ollama.

    The Ollama check sits at the end of on_ready; an extra @bot.event inserted in
    the middle of the handler swallowed it without any syntax error.
    """
    monkeypatch.setitem(CFG.raw, "guild_id", 0)
    monkeypatch.setattr(main, "HEARTBEAT", {"state": "loading_cogs"})

    async def run():
        bot = main.build_bot()
        health_probe = AsyncMock(return_value=True)
        try:
            with patch.object(bot.tree, "sync", AsyncMock(return_value=[])), \
                 patch("main.OLLAMA.health", health_probe):
                await bot.on_ready()
        finally:
            await bot.close()
        return health_probe

    health_probe = asyncio.run(run())

    assert main.HEARTBEAT["state"] == "gateway_connected"
    assert health_probe.await_count == 1


def test_main_module_owns_a_heartbeat_loop():
    """_main must start and stop the beat task (checked statically: no network here)."""
    src = Path(main.__file__).read_text(encoding="utf-8")
    assert "health.beat_forever(HEARTBEAT)" in src
    assert "beat_task.cancel()" in src


def test_probe_cli_exit_codes(tmp_path):
    """`python3 -m core.health` is what compose runs — its exit code is the verdict."""
    env = dict(os.environ, HEALTH_FILE=str(tmp_path / "health.json"))

    missing = subprocess.run([sys.executable, "-m", "core.health"], cwd=ROOT,
                             env=env, capture_output=True, text=True)
    assert missing.returncode == 1
    assert "no heartbeat" in missing.stdout

    health.write("gateway_connected", env["HEALTH_FILE"])
    fresh = subprocess.run([sys.executable, "-m", "core.health"], cwd=ROOT,
                           env=env, capture_output=True, text=True)
    assert fresh.returncode == 0
    assert "gateway_connected" in fresh.stdout
