# CandyResolver change notes (for the Candyflix developer / LLM)

## What went wrong
- A task (`task_id`) is kept for 24 h (`expires_at`). But the stream links inside it come from the sources, and their tokens
  are short-lived: the Vertex source's link was a JWT valid for **4 hours** and bound to the VPS's IP range. Some source domains
  also rotate.
- Candyflix came back after ~7 h, the task still said "valid", so it asked for a download of an old stream link.
  The source answered **401** (or the domain was gone) and the download **failed**.
- Candyflix received no HTTP 401. `POST /v1/tasks/{id}/library` returns 202 at once and the download runs in the
  background; the failure only appears as `status: "failed"` when polling `GET /v1/library/{id}`. Candyflix retried the same dead link
  over and over: the loop.
- `POST /v1/resolve` never reuses a finished task: a new resolve always asks the sources again and returns fresh links.
  Only `GET /v1/tasks/{task_id}` returns the old stored links.

## What is new (CandyResolver)
1. **Per-link expiry.** Each stream has `expires_at` (from the token; `null` = unknown, lives as long as the task).
2. **Dead links are not offered.** `GET /v1/tasks/{id}` leaves out streams that are past their expiry or that a download found dead.
   A source whose links are all gone has `status: "expired"`. The task turns `status: "expired"` only when NO link is left
   (one dead source never expires the others). The task's `expires_at` = when its last live link stops working.
3. **Fast, clear refusal.** `POST /v1/tasks/{id}/library` on such a stream returns **HTTP 410** with
   `{"detail": {"code": "source_link_expired", "message": "..."}}` instead of starting a download that will fail.
4. **Failed because the source refused (401/403/404/410):** the library item gets `status: "failed"` and
   `error_code: "source_link_expired"`, and that stream is marked dead for everyone using the task.
5. Also new earlier: `max_sources` in `POST /v1/resolve`, `POST /v1/tasks/{id}/sources` (`{"count": n}`) to find more sources,
   `GET /v1/library/{id}/audio` (audio only), `duration` / `duration_s` on library items, and per-source rank in the admin panel.

## New: refresh only some sources of a task
`POST /v1/tasks/{task_id}/refresh` body `{"sources": ["Vertex", "Server 12"]}` (names exactly as in the task's `sources[].source`, case-insensitive),
and/or `{"expired": true}` (every source whose links all stopped working) / `{"failed": true}` (every source whose run failed). Only those sources are looked up
again and replaced (new links, new stream ids); the others are untouched. The task keeps its id, goes to `running`, expiry renewed. Query: `wait=first|all`, `wait_s`.
409 while still running, 410 if the task expired (resolve again), 422 for an unknown name. This is the right reaction to `source_link_expired`:
refresh that source (or `{"expired": true}`) instead of resolving the whole task again.

## New: live relay (play without saving anything)
`POST /v1/tasks/{task_id}/live` body `{"stream_id": "str_..."}` -> `{"id": "liv_...", "format": "hls"|"file", "stream_url", "stream_url_expires_at", "reused"}`.
Give `stream_url` to the player (hls.js / native HLS for `format: "hls"`; a plain `<video src>` for `"file"`, Range works). Nothing is saved on the server; candyresolver
fetches from the source with the right IP and headers. Call `DELETE /v1/live/{id}` when the player stops (frees a slot). Asking again for the same stream returns the same session with a fresh link.
Start errors: `410 source_link_expired` (refresh that source first), `429 live_capacity` (retry after the Retry-After seconds), `503 live_disabled`.
While playing, failed requests carry HTTP status + header `X-Candy-Error` + JSON body `{"code","message"}` (hls.js: `data.response.code` / `data.response.text`):
- `source_link_expired` (410): `POST /v1/tasks/{task}/refresh {"sources": ["<source>"]}` (or `{"expired": true}`), take the new stream id from the refreshed task, `POST .../live` again, load the new `stream_url`, seek to the old time. Do NOT retry the same link.
- `live_session_gone` (404): `POST .../live` again with the same stream_id, load the new url, seek to the old time.
- `upstream_timeout` / `upstream_error` / `upstream_rate_limited` (504/502/503): temporary; the relay already retried once; let the player retry (hls.js does), after ~3 failures offer another source.
- `upstream_not_found` (404 on one segment), `upstream_bad_playlist`, `upstream_blocked`: that source is unhealthy; try another stream of the task.
Refresh before `stream_url_expires_at` for a seamless long session. Each viewer uses the stream's bitrate of the VPS's bandwidth; the server limits simultaneous different streams (`LIVE_MAX_SESSIONS`).

## What Candyflix should do
- On HTTP 410 `source_link_expired`, or a library item with `error_code == "source_link_expired"`:
  **never retry the same stream.** Pick another stream or source from the same task, else
  `POST /v1/tasks/{id}/sources`, else `POST /v1/resolve` again.
- If a task is `expired`, call `POST /v1/resolve` again for a new task.
- Limit retries (2-3), then show an error. Never loop on the same link.
- For long watching sessions use the library `url` / `stream_url` (our own domain), not raw source links.

The up-to-date schema is at `GET /openapi.json` on the running server (restart/redeploy first).
