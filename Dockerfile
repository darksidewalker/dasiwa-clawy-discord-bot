# ── Build stage — install dependencies into a clean layer ────────────
# Python 3.15 line on Debian 13 (trixie). Do NOT move to 3.16 until discord.py
# stops calling asyncio.iscoroutinefunction() — that alias is removed in 3.16
# and is already deprecated in 3.14/3.15 (see the DeprecationWarnings raised
# from discord/client.py and discord/ext/commands/core.py).
# aiohttp ships cp315 wheels, so no compiler is needed in either stage.
FROM python:3.15-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────
FROM python:3.15-slim

# UID 568 = TrueNAS default apps user.
# Also works on standard Docker/Podman (uid just won't map to a named system user,
# which is fine — what matters is the number matches volume ownership).
RUN useradd -m -u 568 -g 0 bot

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY main.py   ./
COPY core/     ./core/
COPY cogs/     ./cogs/

# Bake default configs into the image under /app/defaults/
# The entrypoint copies them to /app/config/ on first start if config is missing.
# This means the container starts cleanly even if the volume is empty —
# it self-initialises rather than crashing.
COPY config/   ./defaults/config/

# Runtime directories — will be overridden by volume mounts.
# Owned by 568 so TrueNAS volumes (also owned by 568) are writable.
RUN mkdir -p config data \
    && chown -R 568:0 /app

# Entrypoint script handles first-run config seeding and clear error messages.
# --chmod=755 sets the execute bit atomically in the COPY layer so it cannot
# be skipped by build cache and is unaffected by the source file's permissions.
COPY --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh

USER 568

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEALTH_FILE=/app/data/health.json \
    PYTHONPATH=/app

ENTRYPOINT ["/docker-entrypoint.sh"]
