# candyresolver

TMDB id in -> stream URLs from your sources out. Optionally download a stream to your own server (library) and get a
signed link on your own domain that auto-deletes after N hours.

## One-click start (Docker)
- Windows: double-click `start.bat`  |  Linux/macOS/WSL: `./start.sh`
- It asks for your TMDB API key once, builds, starts API + PostgreSQL, and prints your **admin token**.
- API docs: http://localhost:8000/docs
- Stop: `docker compose down`   Logs: `docker compose logs -f api`   Update: `docker compose up -d --build`
- Data (secrets + downloaded files) lives in the `candy_data` volume. To use a bigger disk, edit the volume line in `docker-compose.yml`.
- Put it behind HTTPS (Caddy / nginx / Cloudflare Tunnel) and set `PUBLIC_BASE_URL` in `.env` so file links use your domain.

## Admin UI
Open **http://localhost:8000/ui** and sign in with the admin token. You can:
- **Sources:** create / edit / disable / delete sources; edit scripts in the browser (every save is a new version, one click to activate or roll back); **test-run** a script against any TMDB id and see the result or the error.
- **API keys:** create (shown once), enable/disable, change rate limits.
- **Library:** live download progress, delete files early.
- **Tasks:** recent requests with per-source status, timing and errors.
- **Overview:** counts, library size, free disk.
The page is public but does nothing without the token; keep the whole service behind HTTPS if it's reachable from the internet.

## Without Docker
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo "TMDB_API_KEY=..." > .env
uvicorn app.main:app --reload     # admin token is printed on first start
```

## First steps
```bash
A='-H X-Admin-Token:YOUR_ADMIN_TOKEN -H Content-Type:application/json'

curl -X POST localhost:8000/admin/api-keys $A -d '{"name":"my-site"}'        # key shown once
curl -X POST localhost:8000/admin/sources $A -d '{"name":"internet-archive"}'
python - <<'PY' > /tmp/body.json
import json; print(json.dumps({"code": open("examples/internet_archive.py").read()}))
PY
curl -X POST localhost:8000/admin/sources/1/scripts $A -d @/tmp/body.json
curl -X POST localhost:8000/admin/sources/1/test $A -d '{"tmdb_id":10378,"type":"movie"}'   # debug a script
```

## Using the API (header `X-API-Key: cr_...`)
```bash
# resolve (tv: add "season" and "episode")
curl -X POST "localhost:8000/v1/resolve?wait_s=10" -H "X-API-Key: $K" -H Content-Type:application/json \
     -d '{"tmdb_id":10378,"type":"movie"}'                  # -> task_id + sources[].streams[] (each has an id)
curl localhost:8000/v1/tasks/tsk_...  -H "X-API-Key: $K"    # same task again, any time before it expires

# library: download ONE stream of that task to our server, delete 24h after it finishes
curl -X POST localhost:8000/v1/tasks/tsk_.../library -H "X-API-Key: $K" -H Content-Type:application/json \
     -d '{"stream_id":"str_...","ttl_hours":24}'            # -> lib_... status "queued"
curl localhost:8000/v1/library/lib_... -H "X-API-Key: $K"   # poll: status, progress, and when "ready": a signed `url`
curl -X DELETE localhost:8000/v1/library/lib_... -H "X-API-Key: $K"   # delete early / cancel
curl localhost:8000/v1/library -H "X-API-Key: $K"           # everything this key has downloaded
```
Library status: `queued -> downloading -> ready` (or `failed`), later `expired` / `deleted`.
You can request more streams of the same task later; asking for the same stream again reuses the file and extends its time.
`GET /v1/library/{id}` returns a fresh signed link each call, so the site just asks again when a link expires.

## How file protection works
Files are only served from `/f/<id>/<name>?exp=...&sig=...` on your domain: an HMAC-signed link that expires
(default 6h, never past the file's delete time). Range requests work, so seeking is instant. The original source URL is never returned in library mode.

## Writing a source script
```python
def resolve(ctx):          # may also be `async def`
    # ctx: title, original_title, alt_titles, year, imdb_id, season, episode, episode_title, ...
    return {
      "streams":   [{"url": "https://...", "quality": "1080p", "format": "mp4", "size": 123,
                     "headers": {"Referer": "..."}, "extra": {}}],
      "subtitles": [{"lang": "en", "url": "https://...", "format": "vtt"}],
      "audio":     [{"lang": "en"}],
    }
```
`print()` is safe. Raise an exception to report an error; it's stored on that source's run.

## Script sandbox
Fresh process per run, temp dir deleted after, scrubbed env (no secrets), memory/CPU/file limits, hard timeout + process-group kill,
output cap. In Docker, scripts also run as the unprivileged user `nobody`, so they can't read secrets or the library.
Not restricted: network access. (Bubblewrap / network policies can be added later.)

## Limits to know
- Downloads and tasks run inside the API process; a restart marks running ones `failed` (re-request them).
- Rate limiting is in-memory (single worker). Tables are auto-created; switch to Alembic before changing the schema.
- Settings: `LIBRARY_TTL_HOURS`, `LIBRARY_MAX_SIZE_GB`, `LIBRARY_CONCURRENCY`, ... (see `.env.example`).

Tests: `python -m tests.smoke_runner` and `python -m tests.smoke_units`
