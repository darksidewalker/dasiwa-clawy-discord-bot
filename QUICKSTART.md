# Quick Start

## Docker / TrueNAS

See **[DOCKER.md](DOCKER.md)** for container deployment. Short version:

```bash
cp .env.example .env
$EDITOR .env            # paste DISCORD_TOKEN
docker compose up -d --build
docker compose logs -f clawy
```

TrueNAS users: use `docker-compose.truenas.yml` and configure everything via
environment variables in the Apps UI — no file editing needed.

---

## Local install — Linux / macOS

After extracting the zip, run:

```bash
bash install.sh
```

The installer will automatically:
- Fix its own executable permissions
- Fix start.sh executable permissions
- Create the virtual environment
- Install all dependencies

If you get "Permission denied", just run it with `bash` explicitly (shown above).

After installation:
```bash
./start.sh
```

## Local install — Windows

```
install.bat
```

No permission issues on Windows.

---

**Full documentation:** See `README.md` (local install) or `DOCKER.md` (containers).
