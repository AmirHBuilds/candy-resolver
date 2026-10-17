#!/usr/bin/env bash
# One-click start (Linux / macOS / WSL).
set -e
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo "Docker is not installed. Install Docker first."; exit 1; }
if [ ! -f .env ]; then
  read -rp "Paste your TMDB API key: " KEY
  echo "TMDB_API_KEY=$KEY" > .env
fi
docker compose up -d --build --wait
echo
echo "candyresolver is running: http://localhost:${PORT:-8000}/docs"
echo -n "Admin token: "
docker compose exec -T api python -c "from app.config import settings, ensure_secrets; ensure_secrets(); print(settings.admin_token)"
