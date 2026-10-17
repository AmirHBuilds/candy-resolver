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
- Each request returns after at most `wait_s` seconds even if nothing changed (you just call again with the same `after`), so a dropped connection or a restart is never a problem: a task can be re-read at any time (see *Tasks: what expires*).
- `wait=first` returns when a source with streams exists; if no source finds anything it returns when the task is done.
- **Starred sources:** star the sources you trust in the admin panel (Sources -> click the star). `wait=first_starred` answers as soon as a **starred** source has streams, ignoring faster unstarred ones. Fallback so you never wait for nothing: once every starred source has finished without streams (or none are starred), it returns as soon as any source has streams. Every source in a response has `"starred": true/false`.
- Plain polling still works (`GET /v1/tasks/{id}` with no `wait_s`), but each call counts toward the key's rate limit (default 60/min), so prefer long polling.
- Long polling holds no database connection while waiting, and wakes instantly when a source finishes (with a once-a-second safety re-check).
- Tasks that were running when the server stopped are marked `failed` on restart.

## Export / import sources
Admin -> Sources -> the download / upload icons above the list. **Export** downloads one JSON file with the chosen sources: settings (name, public name, rank, star,
timeout, browser flag...) and scripts, either the **active version only** or **all versions** (full backup). Nothing server-specific is in the file (no ids, history or
statistics). Scripts may contain keys or passwords you typed into them, so keep the file private.
**Import** reads such a file, checks every script like an upload (syntax, `resolve(ctx)`), and shows a **preview** before anything changes. If a source with the same name
exists you choose: **skip** it, **update** it (settings, and only the script versions it does not have yet are added; the imported active script becomes active), or import
it **as a copy** (named "name (imported)"). A bad source is reported and left out, the others still import. Only import files you trust: the scripts run on your server.
API: `GET /admin/sources/export?include=active|all&ids=1,2`, `POST /admin/sources/import` `{"data": <file>, "mode": "skip|update|copy", "dry_run": true}`.

## Telegram alerts (one topic per kind of message)
Admin -> **Telegram**. Sends important errors and events to a Telegram group, each kind into its own **topic**:
**Source failures** (a source script failed, or a whole task found nothing), **Download failures** (including expired links), **Server errors** (Python errors,
log level ERROR), **Warnings** (log level WARNING, off by default), **Admin actions** (cleanups, deletes, kills) and **System** (server start / stop, low disk space).
Setup: 1) create a bot with @BotFather and put its token in the panel; 2) add the bot to your group and make it admin (so it can post in topics);
3) send a message inside each topic and press **Find chats and topics** to read the group id and topic ids; 4) pick a topic id for each kind, Save, press **Test**.
An empty topic id = the group's General chat. Each kind can also go to a different chat. The same error is sent once per "same error again after" seconds
(default 600; the next message says how many were held back), at most 20 messages per minute, and messages are sent from a queue, so alerts never slow the server.
**Low disk space:** a warning when free space (on the disk that holds the library) drops below a limit you choose (default **2 GB**; 0 = off; checked every 5 minutes),
repeated every 6 hours while it stays low, and a message when it is OK again. **Source keeps failing:** one message when a source fails 5 runs in a row (configurable, 0 = off) and one when it works again.
**Daily report** (own topic, on by default, 08:00 in the time zone you set): tasks (done / failed), every source's runs, share that found streams and average time (🔴 failing, 🟠 mostly empty, 🟢 fine),
downloads (finished, size, failed and why), library size, free disk, server uptime / load / RAM, last automatic clean, and the number of server errors logged. **Send the report now** tests it.
Tokens, signed-link secrets and API keys are removed from every message. Settings are stored in `DATA_DIR/telegram.json` (private file). You can also give first-time values with
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SERVER_NAME`. If your VPS cannot reach api.telegram.org, set "api_base" (API: `PUT /admin/telegram`) to your own relay.
API: `GET/PUT /admin/telegram`, `POST /admin/telegram/test`, `GET /admin/telegram/updates`.

## Refresh button and auto refresh (Overview and System)
Both pages have a **Refresh** button and an **Auto refresh** selector (Off, 2 s, 5 s, 10 s, 30 s, 1 min; the default stays 5 s, your choice is remembered in the browser).
On Overview it updates everything: live numbers and charts, the problems banner, counters, source health and recent tasks (the checks are re-read by the button, and at most every 30 s
by the timer); on System: live numbers, charts, CPU cores, processes and host (the checks by the button). Open task cards stay open, and nothing is requested while the browser tab is hidden.

## Automatic cleaning
- **In the app (on by default):** admin -> Files on disk -> Clean disk -> **Automatic clean**. Every N hours (default 6) it runs the same clean as the "Clean disk" button
  (orphan library folders, old script/browser temp, stale locks, package caches; plus, if you leave it ticked, the database tidy: expire what is due, forget failed / expired
  entries, purge old tasks). With "extra runs when the disk is nearly full" (under 7 % or 1 GB free) it also runs in between, at most every 15 minutes. The panel shows the last result
  and the next run. Telegram (System topic) hears about it only when it freed 100 MB or more, hit errors, or the disk was nearly full. The first run happens 10 minutes after a start.
  API: `GET/PUT /admin/autoclean`.
- **On the VPS itself:** `scripts/host_cleanup.sh` cleans what the app cannot reach: stopped containers, dangling/unused Docker images and build cache (never volumes), giant container logs,
  the systemd journal, apt caches and unneeded packages, old snap revisions, rotated and oversized logs, old temp files, pip/npm caches, trash and crash dumps.
  `sudo ./scripts/host_cleanup.sh --dry-run` shows what it would do; `sudo ./scripts/host_cleanup.sh --install` runs it every Sunday 04:17 (`--daily`, `--aggressive`, `--app-clean`; `--uninstall` removes it).
  See DEPLOY.md.

## Cleaning the whole host (VPS), not only candyresolver
The app runs inside Docker, so it cannot see the VPS's own disk (Docker images and build cache, system logs, apt, temp files...). Giving a container that runs third-party
scripts access to the host would be unsafe, so there is a small **host agent** instead:
1. Update `docker-compose.yml` (it now mounts `./hostclean` into the app as `/hostclean`) and run `docker compose up -d`.
2. Once, on the VPS: `sudo ./scripts/host_cleanup.sh --install-agent` (a cron job, checks every minute; `--uninstall-agent` removes it).
3. In the admin panel: Files on disk -> Clean disk -> tick **Also clean the host**, and/or tick the same option under **Automatic clean**.

How it works: the app drops a tiny request file (`{id, aggressive, dry_run}`) into `./hostclean`; the agent (root, on the host) reads nothing from it except those two true/false flags,
runs `scripts/host_cleanup.sh`, and writes `status.json` / `last.json` back. The container gets no access to the host. The panel shows the agent state, the result and a log
(**Scan** with the host option = a dry run on the VPS). What the host clean does and never does: see `scripts/host_cleanup.sh` (never Docker volumes, running containers or images in use);
**aggressive** also removes all unused Docker images older than 7 days. Automatic runs (also the extra runs when the disk is nearly full) include the host clean when enabled,
and Telegram (System topic) hears about it when it freed 100 MB or more or failed. API: `GET /admin/hostclean`, `POST /admin/hostclean/run`, `host` / `host_aggressive` in
`POST /admin/disk/clean` and `PUT /admin/autoclean`, `?host=true` on `POST /admin/disk/scan`.

## Clearing things
- **Library -> Clear history** now deletes EVERYTHING in the library: every entry of every kind (videos, HLS, subtitle packs), their files, and it cancels running
  downloads. (Before, it only removed failed / expired / deleted rows and left subtitle entries.) The old light behaviour is **System -> Maintenance -> Forget finished entries**.
- **Files on disk -> Clean disk** removes leftovers nothing needs: library folders no live item points at, stray files in the library folder, old script sandboxes and
  browser profiles in the temp folder (older than 30 min), stale virtual-display locks, and pip/apt caches. **Scan** shows what would go and deletes nothing.
  The option "Also tidy the database" first expires what is due, forgets failed / expired entries and purges old tasks. Running downloads, files written in the last
  minutes, symlinks and the data folder (secrets) are never touched. API: `POST /admin/disk/scan`, `POST /admin/disk/clean` `{"database": true}`.
  It cannot clean Docker's own logs/images (outside the container): see DEPLOY.md.

## Live log in "Test run"
Admin -> Sources -> Test run shows a **live log** while the script runs: everything the script `print()`s (and logging / tracebacks), plus what the
runner does (TMDB lookup, waiting for a browser slot, memory use, how it ended), each line with its time. **Follow** keeps the newest line in view,
**Copy** copies it all, **Stop** kills the script. **Verbose browser log** also prints every Playwright/patchright call (`DEBUG=pw:api`), very detailed.
API behind it: `POST /admin/sources/{id}/test/start` -> `{test_id}`, then `GET /admin/tests/{test_id}?after=<next>` (new lines + `result` when done),
`POST /admin/tests/{test_id}/cancel`. Logs are kept in memory for an hour. The old blocking `POST /admin/sources/{id}/test` still works.

## Source rank and "find only N sources"
**Rank (admin panel -> Sources -> Rank):** `1` = tried first, `2` = second, ... Sources without a rank go last (by id). Stars stay as they were
(`starred_first` in a request puts starred sources before everything else; rank orders within each group). The Sources page also shows each
source's **last response time**, its **average response time** (last 50 runs that answered; failed runs are left out) and how often it found streams.
The same numbers are in `GET /admin/sources` (`rank`, `last_ms`, `avg_ms`, `found_rate`, `stats_runs`).

**Ask only for the best source(s):** add `max_sources` to the resolve request. Sources are tried in rank order and only as many run as are still
needed: `1` runs the #1 source, and only if it finds nothing does #2 start, and so on. Omit it to ask every source (the default).
```bash
curl -X POST "localhost:8000/v1/resolve?wait=all" -H "X-API-Key: $K" -H Content-Type:application/json \
     -d '{"tmdb_id":10331,"type":"movie","max_sources":1}'
```
**Later, find more for the same task:**
```bash
curl -X POST "localhost:8000/v1/tasks/tsk_.../sources?wait=first" -H "X-API-Key: $K" -H Content-Type:application/json -d '{"count":1}'
```
It finds `count` MORE sources (the next ones in rank order that were not asked yet), adds them to the same task, and returns the task like `resolve` does
(`wait=first` = as soon as one of the new sources has streams, `wait=all` = when done). Task fields: `sources_wanted` (the target),
`sources_found` (sources that returned streams), `has_more` (true = sources are left to ask). `409` while the task is still running, `410` once its results
expired; with nothing left to ask the task comes back unchanged. The new streams expire with the task.

## Live relay: play a stream through candyresolver, nothing saved (`POST /v1/tasks/{id}/live`)
Source links are tied to the VPS's IP and need special headers, so a player on another machine cannot fetch them itself. The live relay fetches the stream from the source
(from the VPS, with the stream's headers) and gives the player an ordinary signed link on your own domain. No disk, no download, playback starts at once.
```bash
curl -X POST "localhost:8000/v1/tasks/tsk_.../live" -H "X-API-Key: $K" -H Content-Type:application/json -d '{"stream_id": "str_..."}'
# -> {"id": "liv_...", "format": "hls", "stream_url": "https://.../l/liv_.../0.m3u8?exp=..&sig=..", "stream_url_expires_at": "...", "reused": false, ...}
```
- **HLS streams** become an HLS playlist (every playlist, segment, key and subtitle link is rewritten to our domain; adaptive qualities keep working; AES-128 keys work).
  **Non-HLS streams** (a plain mp4...) are relayed as the file, with Range support so seeking works. `DELETE /v1/live/{id}` when the player stops frees its slot at once.
- **Cost:** RAM is a few MB per viewer (segments are streamed chunk by chunk, never held); the real cost is bandwidth: one viewer = the stream's bitrate (about 2-3.5 GB per hour at 1080p)
  out of the VPS. `LIVE_MAX_SESSIONS` (default 10) limits how many different streams are watched at once (a viewer that was idle for 2 minutes does not count; the same stream is shared by
  one session). Over the limit: `429 live_capacity`. `LIVE_ENABLED=false` switches it off.
- **The link expires with the source's:** `stream_url_expires_at` is never later than the source's own link (usually ~4 h), so you know when to refresh.
- **Errors the player can see** (HTTP status + header `X-Candy-Error` + JSON `{"code","message"}`; the header is readable from JavaScript):
  `source_link_expired` (410: the source refused the link; the stream is flagged dead, so task views hide it and `refresh {"expired": true}` picks it up) ->
  refresh, start live again, seek to where it was. `live_session_gone` (404: the server restarted or the session ended) -> just call `POST .../live` again.
  `upstream_timeout` (504), `upstream_error` (502), `upstream_rate_limited` (503 + Retry-After): the source is struggling; one automatic retry already happened, the player's own retries apply.
  `upstream_not_found` (404 on a single segment: the stream is NOT written off), `upstream_bad_playlist` (the source sent a web page, not a playlist), `upstream_blocked` (the address is not a public one).
- **Safe by design:** only links that appeared in a playlist we fetched can be requested (numbered, signed), addresses in private networks / localhost / cloud metadata are refused (also in redirects),
  Range headers are validated, and nothing from a source is ever written to disk. Some sources put junk bytes in front of each .ts segment: the relay cuts them.
- Admin -> System -> **Live streams** shows who is watching, bytes sent and errors per stream, with an End button. Sessions live in memory: a restart ends them (players get `live_session_gone`).

## Refresh only some sources of a task: `POST /v1/tasks/{id}/refresh`
Look up again just the sources you name, replacing their results (new links, new stream ids); every other source stays untouched. The task keeps its id, goes back to
`running`, and its expiry is renewed.
```bash
curl -X POST "localhost:8000/v1/tasks/tsk_.../refresh?wait=first" -H "X-API-Key: $K" -H Content-Type:application/json \
     -d '{"sources": ["Vertex", "Server 12"]}'          # the names as shown in the task's sources[].source (case-insensitive)
curl -X POST ".../refresh" ... -d '{"expired": true}'     # every source whose links all stopped working
curl -X POST ".../refresh" ... -d '{"failed": true}'      # every source whose run failed (they can be combined with names)
```
`wait=first` answers as soon as ONE refreshed source has streams, `wait=all` when all are done. `409` while the task is still running, `410` once its results expired,
`422` for a name that is not in the task or a source that was disabled / removed meanwhile (nothing changes then). Only the refreshed sources run, whatever `max_sources` said.
Library items made from the old links are not touched.

## Audio only: `GET /v1/library/{id}/audio`
Streams just the audio of a **ready** library item (file or progressive HLS) from the copy already on disk: nothing is stored, no video bytes are sent.
Same `X-API-Key` as the rest of `/v1`.
```bash
curl -o movie.aac "localhost:8000/v1/library/lib_.../audio" -H "X-API-Key: $K"
curl -o part.mp3  "localhost:8000/v1/library/lib_.../audio?format=mp3&start=600&duration=300" -H "X-API-Key: $K"
```
`format` = `aac` (default; AAC audio is copied without re-encoding), `mp3` or `opus`. `track` picks another audio track (0 = first), `start` / `duration` cut a part (seconds).
Errors: `404` not yours, `409` not finished, `410` gone, `422` no such track / start after the end, `429` too many at once (`AUDIO_CONCURRENCY`, default 2).
It is streamed (no `Content-Length`, no Range) and ffmpeg stops when the client disconnects.

## Link expiry: each stream link has its own life
Source links usually carry a short-lived token (often ~4 h, sometimes tied to the server's IP), far shorter than the task's 24 h. So:
- Every stream has `expires_at` (read from the token's JWT `exp` or an `expires=` parameter; `null` = unknown, it lives as long as the task).
- An expired link, or one that a download found dead, is **not offered** by `GET /v1/tasks/{id}`. A source whose links are all gone shows `status: "expired"`.
- The task's `expires_at` is when its **last live link** stops working. The task itself turns `expired` only when none is left; one dead source never expires the others.
- `POST /v1/tasks/{id}/library` on such a stream answers **410** `{"detail": {"code": "source_link_expired", ...}}` at once instead of starting a doomed download.
- If a download fails because the source answered 401/403/404/410, the item gets `status: "failed"`, `error_code: "source_link_expired"`, and that stream is marked dead.
- What to do then (client): pick another stream/source from the same task, or `POST /v1/tasks/{id}/sources`, or resolve again. Never retry the same link.

## Tasks: what expires, what stays
A resolve result contains source links that go stale within hours, so only the **results** expire; the **task itself is kept**.
- After `TASK_TTL_HOURS` (24) a task's status becomes **`expired`**: `GET /v1/tasks/{id}` still answers **200** with the movie info (`tmdb_id`, `type`, `season`, `episode`, `title`),
  `sources: []` (no stale links, even before the cleanup job runs) and the task's **`library`** items. Library requests on it say "resolve again" (410), because the links they need are gone.
- The task record is deleted only after `TASK_RETENTION_DAYS` (365; `0` = forever). Library items and hosted subtitles live by their own TTL and are not affected.
- `GET /v1/tasks?tmdb_id=&type=&season=&episode=&limit=` lists your tasks (newest first, no results). `GET /v1/tasks/{id}` also includes `library`: every video download made from that task.
- **Library items describe themselves:** they carry `tmdb_id`, `type`, `season`, `episode`, `title` (copied from the task), so they stay meaningful long after the task's results are gone.
  Filter them: `GET /v1/library?tmdb_id=26573&type=movie` (also `season`, `episode`, `status`, `limit`). Items created before this update are filled in on startup when their task still exists.
- The admin panel keeps each task's per-source summary (status, timing) for the health view; only streams and original links are deleted.

## Hosted subtitles for a whole task
Want every subtitle of a movie, not only those of one stream's source? No video needed:
```bash
curl -X POST localhost:8000/v1/tasks/tsk_.../subtitles -H "X-API-Key: $K" -H Content-Type:application/json \
     -d '{"subtitle_langs":["en","fa"],"subtitle_default":"en"}'     # body optional; -> 202 {id, status:"pending", subtitles:[...]}
curl localhost:8000/v1/tasks/tsk_.../subtitles -H "X-API-Key: $K"      # poll until status is not "pending"; works after the task's results expired
```
It fetches the subtitles of **all sources** of the task (starred sources first, so they win when `SUBTITLE_MAX_FILES` cuts the list), converts and hosts them exactly like library subtitles
(same `/s/...vtt` links, same safety rules), and every track says which provider it came from: `{"lang":"en","label":"English","source":"Server A","url":...}`.
Same language and same content from two sources is kept once. Calling it again reuses the existing pack. It lives `SUBTITLE_PACK_TTL_HOURS` (168) and is deleted with its files;
`DELETE /v1/library/{id}` removes it early. Packs do not appear in `GET /v1/library` unless you ask for `?mode=subs`.

## Progressive HLS: play while it downloads
For a player on another site (e.g. CandyFlix): ask for `"progressive": true` and the server downloads the stream as **HLS (playlist + segments)**,
so you get a `stream_url` after ~3 segments (~12 s of video) while the rest keeps downloading. Video is **never re-encoded** (`ffmpeg -c copy`).
```bash
# 1) request it (everything else about /library is unchanged; without "progressive" you still get the single-file download)
curl -X POST localhost:8000/v1/tasks/tsk_.../library -H "X-API-Key: $K" -H Content-Type:application/json \
     -d '{"stream_id":"str_...","progressive":true,"ttl_hours":24}'          # -> lib_..., mode "hls", status "queued"
# 2) poll until "playable" is true (every GET returns a fresh signed stream_url)
curl localhost:8000/v1/library/lib_... -H "X-API-Key: $K"
# 3) give stream_url to the player once playable is true
```
```json
{ "id": "lib_...", "mode": "hls", "status": "downloading", "playable": true, "progress": 0.31,
  "size": 52000000, "total_bytes": 168000000, "download_speed": 4800000, "eta_seconds": 24,
  "stream_url": "https://candy.example.com/h/lib_.../index.m3u8?exp=...&sig=...", "stream_url_expires_at": "...",
  "url": null, "ttl_hours": 24, "delete_at": null }
```
New fields (on **every** library endpoint: POST/GET `/v1/tasks/{id}/library`, GET `/v1/library`, GET `/v1/library/{id}`):
`mode` (`file`|`hls`), `playable`, `stream_url`, `stream_url_expires_at`, `download_speed` (bytes/s, smoothed over ~8 s) and `eta_seconds`
(both `null` unless `status` is `downloading`). `url` is only used by `file` items; `stream_url` only by `hls` items. `progress` never goes backwards
and is only 1.0 when `status` is `ready` (`ready` = ffmpeg finished cleanly **and** the playlist has `#EXT-X-ENDLIST`).
`size`/`total_bytes` for HLS are the bytes of the segments (total is a projection until it is done).

Player (hls.js) - **`startPosition: 0`** is needed because the playlist is still growing:
```js
const res = await fetch(`${BACKEND}/library/${id}`).then(r => r.json());     // your backend calls candyresolver with the API key
if (res.playable) {
  const hls = new Hls({ startPosition: 0 });
  hls.loadSource(res.stream_url);
  hls.attachMedia(video);
}
```
While `status` is `downloading` the seek bar only reaches the part that is downloaded so far (hls.js keeps loading new segments as they appear);
once `ready` the whole movie is seekable. Use `?exp&sig` links only for the player: they are bearer links (valid 12 h by default, never past the file's delete time).
- **Sources:** plain mp4/mkv links and HLS (.m3u8) sources both work. Video must be **h264** (8-bit 4:2:0), otherwise the item fails with e.g. `unsupported video codec: hevc`.
  Audio aac/mp3 is copied, anything else is converted to aac (cheap); subtitles and data streams are dropped.
- **Failures:** if ffmpeg fails, the source is cut off early, the limit/disk reserve/timeout is hit or you DELETE the item: ffmpeg is killed, the whole folder is removed,
  the item is `failed` with a short `error`, and its links stop working (no playlist that never ends).
- **Reuse:** the same stream requested again (by anyone with the same key) reuses the running or finished download; a `file` and an `hls` download are separate.
- **CORS:** `/h/...` answers `GET/HEAD/OPTIONS` with `Range` allowed and `Content-Length`, `Content-Range`, `Accept-Ranges` exposed. Set `CORS_ORIGINS=https://candyflix.example`
  (default `*`; a comma-separated list or empty = no cross-origin access). `/f/...` never serves HLS items.
- **Settings:** `PROGRESSIVE_SEGMENT_SECONDS` (4), `PROGRESSIVE_MIN_SEGMENTS` (3), `HLS_LINK_TTL_MIN` (720), `CORS_ORIGINS`, plus the existing
  `LIBRARY_CONCURRENCY`, `LIBRARY_MAX_SIZE_GB`, `LIBRARY_MIN_FREE_GB` and `LIBRARY_HLS_TIMEOUT_MIN` which apply to progressive downloads too.
- The growing playlist is sent with `Cache-Control: no-store`; segments are cacheable. Segments are cut at keyframes, so a source with rare keyframes gives longer segments.

## Subtitles
When a stream goes to the library (file mode **and** progressive HLS), the subtitles of the **same source run** are downloaded too, converted to
**WebVTT** and served from your own domain with signed links, so CandyFlix never depends on the original website. It starts right away, **in parallel with the
video**, and a subtitle problem can never fail or delay the video: that subtitle is just missing. Subtitles are never muxed into the mp4/HLS (separate `.vtt` files).
```bash
curl -X POST localhost:8000/v1/tasks/tsk_.../library -H "X-API-Key: $K" -H Content-Type:application/json \
     -d '{"stream_id":"str_...","progressive":true,"subtitle_langs":["en","fa"],"subtitle_default":"en"}'
```
```json
{ "id": "lib_...", "status": "downloading", "subtitles_status": "pending",
  "subtitles": [
    {"lang": "en", "label": "English",   "format": "vtt", "default": true,  "url": "https://candy.example.com/s/lib_.../1.vtt?exp=...&sig=..."},
    {"lang": "fa", "label": "Persian",   "format": "vtt", "default": false, "url": "https://candy.example.com/s/lib_.../3.vtt?exp=...&sig=..."} ] }
```
- **Fields** (on every library endpoint): `subtitles` = list of `{lang, label, format: "vtt", url, default}` that **grows as files finish**, and `subtitles_status`:
  `pending` (still fetching) | `done` (all fetched) | `partial` (some failed/skipped) | `none` (nothing available/enabled, or all failed). Poll `GET /v1/library/{id}` until it is no longer `pending`.
- **Request options** (override the settings for this request; ignored when the same stream is already in the library, which reuses that item's subtitles):
  `subtitles` (`true`/`false`), `subtitle_langs` (`["en","fa"]`; `[]` = all), `subtitle_default` (`"en"`: that language's first finished track gets `default: true`; otherwise none is marked).
- **Settings:** `SUBTITLES_ENABLED` (true), `SUBTITLE_MAX_FILES` (30), `SUBTITLE_MAX_BYTES` (2 MB), `SUBTITLE_TIMEOUT_S` (15), `SUBTITLE_CONCURRENCY` (4),
  `SUBTITLE_LANGS` (allow-list, empty = all), `SUBTITLE_DEFAULT_LANG`, `SUBTITLE_FFMPEG_FALLBACK` (true).
- **Conversion** is pure Python (cheap): the format is detected from the **content**; SRT -> VTT (header, comma -> dot, BOM, line endings, tags cleaned), VTT kept; encodings
  utf-8, utf-16, then cp1256/cp1255/cp1251/cp1252/latin-1 chosen by language; always written as utf-8. HTML error pages, binary, empty and unknown files are skipped.
  ASS/SSA is the only thing that uses ffmpeg (one at a time, tiny input); set `SUBTITLE_FFMPEG_FALLBACK=false` to skip those instead.
- **Languages:** `lang` is normalized to a short code (`eng`/`English`/`en-US` -> `en`, `fas`/`Persian` -> `fa`; unknown = `und`). Labels are readable and unique ("English", "English 2");
  numbering follows the source's order, so a label never changes once you have seen it. Same language + same content/URL is dropped.
- **Download safety:** http/https only; redirects are followed but every hop is checked and **internal/private addresses are refused** (the connection goes to the address that was checked);
  the size cap is enforced while streaming; each file has its own timeout, and slow ones never block the others. A subtitle's own `headers` are used, else the stream's.
- **Route:** `GET /s/{item_id}/{n}.vtt?exp&sig` - same signature as the video routes, strict file names, `Content-Type: text/vtt; charset=utf-8`, CORS like the HLS route
  (`CORS_ORIGINS`), `Cache-Control: private, max-age=3600`. Not served for queued-never/failed/expired/deleted items. Files live in `LIBRARY/<item_id>/subs/<n>.vtt` and are deleted with the item.

Player (the files are on another domain, so `crossorigin="anonymous"` is required on the `<video>`):
```html
<video id="v" controls crossorigin="anonymous"></video>
<script>
const added = new Set();
async function addSubtitles(libId) {
  for (;;) {                                                      // poll until no longer "pending"
    const it = await fetch(`${BACKEND}/library/${libId}`).then(r => r.json());   // your backend calls candyresolver with the API key
    for (const s of it.subtitles) if (!added.has(s.url.split('?')[0])) {
      added.add(s.url.split('?')[0]);
      const t = document.createElement('track');
      Object.assign(t, { kind: 'subtitles', srclang: s.lang, label: s.label, src: s.url, default: s.default });
      document.getElementById('v').appendChild(t);
    }
    if (it.subtitles_status !== 'pending') break;
    await new Promise(r => setTimeout(r, 2000));
  }
}
</script>
```

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

Tests: `python -m tests.smoke_liverelay`, `python -m tests.smoke_hostclean`, `bash tests/test_host_agent.sh`, `python -m tests.smoke_refresh`, `python -m tests.smoke_report`, `python -m tests.smoke_alerts`, `python -m tests.smoke_autoclean`, `bash tests/test_host_cleanup.sh`, `python -m tests.smoke_sourcexfer`, `python -m tests.smoke_notify`, `python -m tests.smoke_notify_hooks`, `python -m tests.smoke_diskclean`, `python -m tests.smoke_testlog`, `python -m tests.smoke_links`, `python -m tests.smoke_audio` (needs ffmpeg), `python -m tests.smoke_ranking`, `python -m tests.smoke_runner`, `python -m tests.smoke_units`, `python -m tests.smoke_hls`, `python -m tests.smoke_waiting`, `python -m tests.smoke_progressive`, `python -m tests.smoke_subtitles`, `python -m tests.smoke_tasks`

## Admin panel (`/ui`)

| Page | What it does |
|---|---|
| **Overview** | Live CPU / memory / disk / network (5 s refresh) with 30-minute charts, errors in the last hour, a live log, source health, recent tasks and a red box when something the resolver needs is missing (ffmpeg, Xvfb, Chrome, shm…). |
| **Library** | Every download with search + status filter, errors one click away, play/copy link, delete/cancel, "cancel running", "clear history". |
| **Files on disk** | The real folders in the library volume matched with the database: size, file count, last write, orphan detection, expand to see the files inside, open/copy a signed link, delete one folder, delete all orphans, delete everything. |
| **Logs** | Live tail of everything logged (requests, source runs, downloads, ffmpeg errors). Level filter, text search, pause, download as .txt, clear. Secrets (tokens, signatures, API keys) are masked. Kept in memory (last 3000 lines). |
| **System** | Per-core CPU, processes started by the resolver (ffmpeg / Chrome / Xvfb) with a Kill button, checks, host info and maintenance jobs (expire files now, sweep orphans, purge tasks, cancel downloads…). |
| **Settings** | The effective configuration (read-only, secrets hidden) with the environment variable names. |

## Queue, browser sources and reuse

* **Task queue** - `MAX_CONCURRENT_TASKS` (default 2) tasks run at once. The rest are accepted immediately and wait in
  first-in-first-out order: `status: "queued"` and `queue_position` (1 = next) in `POST /v1/resolve`, `GET /v1/tasks` and
  `GET /v1/tasks/{id}`. Beyond `MAX_QUEUED_TASKS` (default 20) the API answers `429` with `Retry-After`. `0` = unlimited.
* **Browser sources** - each source has a *Browser source* switch (panel -> Sources). It is detected from the script's
  imports (playwright, selenium, pyvirtualdisplay...) when you upload/activate a script, and you can override it; your choice
  is never overwritten (use *auto-detect again* to go back). At most `MAX_BROWSER_SCRIPTS` (default 1) browser scripts run at
  once across all tasks; light sources are never held back by it.
* **Peak RAM** - every source run records its peak memory (script + Chrome + Xvfb, shared pages counted once). The panel shows
  it per source and per run, and suggests ticking *Browser source* when an unflagged source used more than 250 MB.
* **Reuse** - `POST /v1/resolve` for the same movie/episode with the same API key while that task is queued or running returns
  the existing task instead of starting another. `POST /v1/tasks/{id}/library` reuses an existing download only if stream,
  quality, `progressive` and the subtitle options (`subtitles`, `subtitle_langs`, `subtitle_default`) are all the same;
  otherwise it starts a separate one. `ttl_hours` is not part of "same": reusing just extends the time.
* **Starred first** - `POST /v1/resolve` accepts `"starred_first": true`: starred (reliable) sources start before the others,
  and get the browser slot first when several browser sources compete. It changes the start order only; `wait=first_starred`
  is separate (it decides when the response is returned). Two resolve requests count as "the same" only if this flag matches too.
