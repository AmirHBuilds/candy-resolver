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
- **Sources:** create / edit / disable / delete sources; edit scripts in the browser (a dropdown loads the bundled examples as starting points) (every save is a new version, one click to activate or roll back); **test-run** a script against any TMDB id and see the result or the error.
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

## Fast first answer + staying up to date
Sources finish at different speeds. Don't wait for the slowest one:
```bash
# 1) answer as soon as ONE source has streams (max 8s here); the others keep running in the background
#    (use wait=first_starred to count only your starred / reliable sources)
curl -X POST "localhost:8000/v1/resolve?wait=first&wait_s=8" -H "X-API-Key: $K" -H Content-Type:application/json \
     -d '{"tmdb_id":10331,"type":"movie"}'          # -> 200 if finished, 202 if still running

# 2) keep asking, but let the server hold the request until something NEW happens (long polling)
curl "localhost:8000/v1/tasks/tsk_...?after=1&wait_s=25" -H "X-API-Key: $K"   # `after` = the `version` you last saw
```
Every response has `status` (`pending` -> `running` -> `done` | `failed`), `version` (+1 each time a source finishes) and
`sources_done` / `sources_total`. Your website's backend loops until `status` is `done` or `failed`:
```js
let t = await resolve({wait: "first", wait_s: 8});          // show t.sources[].streams right away
while (t.status === "pending" || t.status === "running") {
  t = await get(`/v1/tasks/${t.task_id}?after=${t.version}&wait_s=25`);   // returns the moment a source finishes
  show(t);                                                   // new qualities / servers appear as they arrive
}
```
- Each request returns after at most `wait_s` seconds even if nothing changed (you just call again with the same `after`), so a dropped connection or a restart is never a problem: any task can be re-read at any time until it expires.
- `wait=first` returns when a source with streams exists; if no source finds anything it returns when the task is done.
- **Starred sources:** star the sources you trust in the admin panel (Sources -> click the star). `wait=first_starred` answers as soon as a **starred** source has streams, ignoring faster unstarred ones. Fallback so you never wait for nothing: once every starred source has finished without streams (or none are starred), it returns as soon as any source has streams. Every source in a response has `"starred": true/false`.
- Plain polling still works (`GET /v1/tasks/{id}` with no `wait_s`), but each call counts toward the key's rate limit (default 60/min), so prefer long polling.
- Long polling holds no database connection while waiting, and wakes instantly when a source finishes (with a once-a-second safety re-check).
- Tasks that were running when the server stopped are marked `failed` on restart.

## HLS (.m3u8) streams
Return a stream with `"format": "hls"` (or a URL ending in `.m3u8`). In library mode the server remuxes it to a single **mp4 with ffmpeg**
(no re-encoding, so it's fast and uses little CPU), including AES-128 encrypted playlists and separate audio tracks.
For a master playlist it picks the quality the client asks for, default the best:
```bash
curl -X POST localhost:8000/v1/tasks/tsk_.../library -H "X-API-Key: $K" -H Content-Type:application/json \
     -d '{"stream_id":"str_...","quality":"720p"}'      # "quality" is optional and only used for HLS
```
Asking again for the same stream with a different quality creates a separate library file. Progress is estimated from the playlist length.
Not supported: live streams and DRM (Widevine/FairPlay). ffmpeg is installed in the Docker image; without Docker install it yourself.
`examples/hls_template.py` shows what a script returns for an HLS source.

## Script sandbox
Fresh process per run, temp dir deleted after, scrubbed env (no secrets), memory/CPU/file limits, hard timeout + process-group kill,
output cap. In Docker, scripts also run as the unprivileged user `nobody`, so they can't read secrets or the library.
Not restricted: network access. (Bubblewrap / network policies can be added later.)

## Limits to know
- Downloads and tasks run inside the API process; a restart marks running ones `failed` (re-request them).
- ffmpeg reads untrusted playlists: it only gets http/https (no `file:`), but it is not sandboxed like scripts are. Only add sources you trust.
- Rate limiting is in-memory (single worker). Tables are auto-created; switch to Alembic before changing the schema.
- Settings: `LIBRARY_TTL_HOURS`, `LIBRARY_MAX_SIZE_GB`, `LIBRARY_CONCURRENCY`, ... (see `.env.example`).

Tests: `python -m tests.smoke_runner`, `python -m tests.smoke_units`, `python -m tests.smoke_hls`, `python -m tests.smoke_waiting`
