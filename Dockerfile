# ── Build stage — install dependencies into a clean layer ────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install pip dependencies into a prefix directory so we can copy them cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage — minimal image ────────────────────────────────────
FROM python:3.12-slim

# Non-root user for security
RUN useradd -m -u 1000 bot

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY main.py        ./
COPY core/          ./core/
COPY cogs/          ./cogs/

# These directories are mounted as volumes at runtime.
# We create them here so the container starts cleanly even without a mount,
# and so the 'bot' user owns them.
RUN mkdir -p config data \
    && chown -R bot:bot /app

USER bot

# The bot reads config from /app/config and writes the DB to /app/data.
# Both are expected to be mounted as volumes.
# OLLAMA_URL is passed via environment variable or .env file mounted into /app.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "main.py"]
