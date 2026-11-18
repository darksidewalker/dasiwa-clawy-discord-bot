"""Container liveness heartbeat.

Clawy opens a Discord gateway connection; it serves no HTTP port, so a
container healthcheck has nothing to probe. Instead the process *reports*
liveness: an asyncio task stamps a heartbeat file every `HEALTH_INTERVAL`
seconds, and the healthcheck (`python3 -m core.health`) fails once that stamp
goes stale.

Why a timestamp instead of a process check: `restart: unless-stopped` already
sees a dead process. What it cannot see is a process that is alive but makes no
progress — a blocked event loop, a wedged task, a stuck startup. A stale
heartbeat is exactly that case, so the container is marked unhealthy and the
manager can restart it.

Two properties keep this safe in production:

* The stamp is written atomically (tmp + rename), so a probe never reads a
  half-written value, and every failure path is swallowed — monitoring must
  never take the bot down.
* If the configured file is not stampable (data volume mounted read-only, or
  owned by a UID the container does not run as), the heartbeat falls back to
  container-local storage instead of silently disappearing. Without that, a
  perfectly healthy bot would be reported unhealthy forever.

This module imports nothing outside the standard library: the probe has to work
while Discord and Ollama are down.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

# Pure env, never written into config.yaml (same rule as OLLAMA_URL).
HEALTH_FILE = os.environ.get("HEALTH_FILE", "/app/data/health.json")
FALLBACK_HEALTH_FILE = os.environ.get("HEALTH_FALLBACK_FILE", "/tmp/clawy-health.json")
HEALTH_INTERVAL = float(os.environ.get("HEALTH_INTERVAL", "10"))

# A heartbeat older than this means the loop stopped making progress. Keep it
# above 3 * HEALTH_INTERVAL so a single slow beat never marks the container
# unhealthy on its own.
HEALTH_MAX_AGE = float(os.environ.get("HEALTH_MAX_AGE", "90"))

_RESOLVED: str | None = None


def _usable(target: str) -> bool:
    """Can an atomic stamp land here? Probe instead of guessing (perms, ENOTDIR)."""
    probe = f"{target}.probe"
    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(probe, "w", encoding="utf-8"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def health_path() -> str:
    """The file actually being stamped — the configured one, or the fallback."""
    global _RESOLVED
    if _RESOLVED is None:
        _RESOLVED = HEALTH_FILE if _usable(HEALTH_FILE) else FALLBACK_HEALTH_FILE
    return _RESOLVED


def reset() -> None:
    """Forget the cached resolution (tests, or after changing the env)."""
    global _RESOLVED
    _RESOLVED = None


def _target(path: str | None) -> str:
    return health_path() if path is None else path


def write(state: str, path: str | None = None) -> bool:
    """Stamp the heartbeat file with the current time. Never raises."""
    target = _target(path)
    stamp = {"ts": time.time(), "pid": os.getpid(), "state": state}
    tmp = f"{target}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(stamp, fh)
        os.replace(tmp, target)
        return True
    except OSError:
        # Read-only mount, full disk, missing parent — never break the bot.
        return False


def age(path: str | None = None) -> float:
    """Seconds since the last heartbeat, or inf if there is no usable stamp."""
    target = _target(path)
    try:
        with open(target, encoding="utf-8") as fh:
            return time.time() - float(json.load(fh)["ts"])
    except (OSError, TypeError, ValueError, KeyError):
        return float("inf")


def check(max_age: float = HEALTH_MAX_AGE, path: str | None = None) -> tuple[bool, str]:
    """Return (healthy, description) — this is what `python3 -m core.health` prints."""
    target = _target(path)
    current = age(target)
    if current == float("inf"):
        return False, f"no heartbeat stamp at {target}"

    state = "?"
    try:
        with open(target, encoding="utf-8") as fh:
            state = str(json.load(fh).get("state", "?"))
    except (OSError, ValueError):
        pass

    where = target if target == HEALTH_FILE else f"{target} [fallback — {HEALTH_FILE} is not writable]"
    return current <= max_age, f"{state} heartbeat {current:.0f}s old via {where} (max {max_age:.0f}s)"


async def beat_forever(state: dict, path: str | None = None,
                       interval: float | None = None) -> None:
    """Stamp the heartbeat until cancelled.

    `state` is a mutable holder: `state["state"]` carries the current label
    ("starting", "loading_cogs", "gateway_connected", ...), so the healthcheck
    output shows *where* the bot is, not merely that it is alive. The file write
    runs in a worker thread — the event loop must never block on the data volume.
    """
    step = HEALTH_INTERVAL if interval is None else interval
    while True:
        await asyncio.to_thread(write, str(state.get("state", "running")), path)
        await asyncio.sleep(step)


if __name__ == "__main__":
    healthy, detail = check()
    print(f"clawy healthcheck: {detail}")
    raise SystemExit(0 if healthy else 1)
