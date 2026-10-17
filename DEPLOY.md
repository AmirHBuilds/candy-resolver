# Deploying candyresolver on a VPS

## First install
```bash
# Docker + compose plugin installed, then, in the project folder:
cp .env.example .env && nano .env        # set TMDB_API_KEY; recommended: PUBLIC_BASE_URL, CORS_ORIGINS, API_MEM_LIMIT
docker compose up -d --build --wait
docker compose exec -T api python -c "from app.config import settings, ensure_secrets; ensure_secrets(); print(settings.admin_token)"
./deploy_check.sh http://localhost:8000 <admin token>
```
- `API_MEM_LIMIT`: set to about 70% of the VPS RAM (e.g. `1400m` on 2 GB). A browser source needs ~400 MB.
- `POSTGRES_PASSWORD`: on a NEW install set your own. On an existing install do **not** change it (the database keeps the old one).

## Update an existing install (keeps all data)
Data lives in Docker volumes (`pgdata` = database, `candy_data` = secrets + downloads), so rebuilding never touches it.
```bash
docker compose exec -T db pg_dump -U candy candyresolver > backup_$(date +%F).sql   # 1. backup (10 seconds)
# 2. replace the project files with the new version (keep your .env)
docker compose up -d --build --wait                                                  # 3. rebuild + restart
docker compose logs --tail 50 api | grep -i migrat                                   # new columns are added automatically
./deploy_check.sh http://localhost:8000 <admin token>                                # 4. verify
```
Rollback: put the old files back, `docker compose up -d --build`. The new columns are additive, so the old version still runs on the migrated database.
Restore a backup (only if needed): `docker compose exec -T db psql -U candy candyresolver < backup_YYYY-MM-DD.sql`.

## HTTPS (recommended): Caddy in front
Set `BIND_ADDRESS=127.0.0.1` and `PUBLIC_BASE_URL=https://candy.example.com` in `.env`, restart, then:
```
# /etc/caddy/Caddyfile
candy.example.com {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1        # do not buffer streamed audio/video
    }
}
```
Also set `CORS_ORIGINS` to your site (e.g. `https://candyflix.example.com`) instead of `*`.

## Disk space on the VPS
- **Inside the app** (automatic, every 6 h by default): admin -> **Files on disk -> Clean disk -> Automatic clean**. You can also press Scan / Clean disk by hand.
- **The host itself** (Docker leftovers, system logs, apt, temp files): `scripts/host_cleanup.sh`.
```bash
sudo ./scripts/host_cleanup.sh --dry-run        # look first: changes nothing
sudo ./scripts/host_cleanup.sh                  # clean now
sudo ./scripts/host_cleanup.sh --install        # and run it every Sunday 04:17 (add --daily, or --aggressive for ALL unused Docker images older than 7 days)
sudo ./scripts/host_cleanup.sh --uninstall      # stop the schedule
```
It never touches Docker volumes (database, secrets, library), running containers, or images that a container uses. Log of scheduled runs: `/var/log/candyresolver-host-cleanup.log`.
`--app-clean` (with `CANDY_ADMIN_TOKEN` set) also makes the app run its own disk clean first.
- **From the admin panel (Clean disk / Automatic clean, "Also clean the host")**: install the host agent once: `sudo ./scripts/host_cleanup.sh --install-agent`
  (the compose file mounts `./hostclean` into the app; the agent checks it every minute and runs the same host cleanup). Remove it with `--uninstall-agent`.

## Daily operation
- Logs: `docker compose logs -f api` (rotated: 5 x 10 MB per container)
- Disk: library files auto-delete after `LIBRARY_TTL_HOURS`; admin panel -> Files on disk shows usage.
- Firewall: open only 80/443 (and SSH). With `BIND_ADDRESS=127.0.0.1` port 8000 is not reachable from outside.

What changed for API clients: see `docs/CANDYFLIX_NOTES.md`.
