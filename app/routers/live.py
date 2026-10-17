"""Live relay endpoints. The logic is in liverelay.py; this turns it into HTTP.
  POST   /v1/tasks/{task_id}/live   start (or reuse) a live stream of one stream of a task -> a signed stream_url on our domain
  DELETE /v1/live/{id}              the player is done: free its slot
  GET    /l/{session}/{n}.{ext}     what the player fetches (playlists, segments, keys, or the file itself)"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from ..auth import require_admin, require_api_key
from ..config import settings
from ..db import SessionLocal, get_db
from ..fileutil import is_hls
from ..hlsserve import cors_headers
from ..live_http import fetcher
from ..liverelay import DEFAULT_UA, NAME, SID, Capacity, LiveRegistry, LiveReply, RelayError, _error_reply, file_ext, serve, url_is_safe
from ..models import ApiKey, SourceRun, Stream, Task, as_utc, public_label, utcnow
from ..resolver import is_expired
from ..schemas import LiveOut, LiveRequest
from ..signing import sign, verify

log = logging.getLogger("candy.live")
router = APIRouter(tags=["live"])
admin_router = APIRouter(prefix="/admin", tags=["admin-live"], dependencies=[Depends(require_admin)])
registry = LiveRegistry(max_active=settings.live_max_sessions)
_DROP_HEADERS = {"host", "content-length", "accept-encoding", "range", "connection", "transfer-encoding"}
_EXPIRED_DETAIL = {"code": "source_link_expired", "message": "this stream link has stopped working: refresh the source "
                   "(POST /v1/tasks/{id}/refresh), pick another stream, or resolve again"}


def _base(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


@router.post("/v1/tasks/{task_id}/live", response_model=LiveOut)
async def start_live(task_id: str, body: LiveRequest, request: Request, key: ApiKey = Depends(require_api_key),
                     db: AsyncSession = Depends(get_db)):
    """Play one stream of a task live through candyresolver, nothing is saved: you get a signed `stream_url` (HLS, or the plain file for
    non-HLS streams) to give to the player. Asking again for the same stream returns the same session with a fresh link.
    Errors: 410 {"code": "source_link_expired"} (refresh the source), 429 {"code": "live_capacity"} (too many streams at once, retry in a
    few seconds), 503 when the relay is switched off. While playing, the same 410 can come from a segment: see X-Candy-Error."""
    if not settings.live_enabled:
        raise HTTPException(503, {"code": "live_disabled", "message": "the live relay is switched off on this server"})
    task = await db.get(Task, task_id)
    if task is None or task.api_key_id != key.id:
        raise HTTPException(404, "task not found")
    if is_expired(task):
        raise HTTPException(410, "this task's results have expired (the links are stale) - resolve again")
    row = (await db.execute(select(Stream, SourceRun).join(SourceRun, Stream.run_id == SourceRun.id)
                            .where(Stream.id == body.stream_id, SourceRun.task_id == task_id))).first()
    if row is None:
        raise HTTPException(404, "stream not found in this task")
    stream, run = row
    ttl = settings.hls_link_ttl_min * 60
    if stream.expires_at:                                    # the link we hand out never claims to live longer than the source's own
        ttl = min(ttl, int((as_utc(stream.expires_at) - utcnow()).total_seconds()) - 60)
    if stream.dead or ttl < 60:
        raise HTTPException(410, _EXPIRED_DETAIL)

    headers, ua = dict(stream.headers or {}), DEFAULT_UA
    for k in list(headers):
        if k.lower() == "user-agent":
            ua = headers.pop(k)
        elif k.lower() in _DROP_HEADERS:
            headers.pop(k)
    hls = is_hls(stream.format, stream.url)
    ext = "m3u8" if hls else file_ext(stream.format, stream.url)
    try:
        sess, reused = registry.start(api_key_id=key.id, task_id=task_id, stream_id=stream.id,
                                      source=run.public_name or public_label(None, run.source_id), quality=stream.quality,
                                      kind="hls" if hls else "file", entry_url=stream.url, entry_ext=ext, headers=headers, ua=ua, ttl_s=ttl)
    except Capacity:
        raise HTTPException(429, {"code": "live_capacity", "message": "too many live streams at once: try again in a few seconds"},
                            headers={"Retry-After": "10"})
    if not reused:
        log.info("live %s started: %s %s (%s)", sess.id, sess.source, stream.quality, sess.kind)
    exp, sig = sign("live:" + sess.id, ttl)
    return LiveOut(id=sess.id, task_id=task_id, stream_id=stream.id, source=sess.source, quality=stream.quality, format=sess.kind,
                   stream_url=f"{_base(request)}/l/{sess.id}/0.{ext}?exp={exp}&sig={sig}",
                   stream_url_expires_at=datetime.fromtimestamp(exp, timezone.utc), reused=reused)


@router.delete("/v1/live/{live_id}", status_code=204)
async def stop_live(live_id: str, key: ApiKey = Depends(require_api_key)):
    """The player stopped: free the slot at once (sessions also expire on their own)."""
    if not registry.end(live_id, key.id):
        raise HTTPException(404, "live stream not found")


# ---------------------------------------------------------------- what the player fetches
def _cors(origin: str | None) -> dict:
    h = cors_headers(origin, settings.cors_origins)
    if "Access-Control-Expose-Headers" in h:
        h["Access-Control-Expose-Headers"] += ", X-Candy-Error, Retry-After"       # so the player's JavaScript can read WHY it failed
    return h


def _respond(reply: LiveReply, method: str) -> Response:
    if reply.stream is not None:
        return StreamingResponse(reply.stream, status_code=reply.status, headers=reply.headers,
                                 background=BackgroundTask(reply.close) if reply.close else None)
    return Response(content=reply.body or b"", status_code=reply.status, headers=reply.headers)


async def _mark_dead(stream_id: str) -> None:
    """The source refused this link: hide the stream everywhere (task view, library requests) and let refresh pick it up."""
    try:
        async with SessionLocal() as db:
            await db.execute(update(Stream).where(Stream.id == stream_id).values(dead=True))
            await db.commit()
        log.info("live: stream %s marked dead (the source refused the link)", stream_id)
    except Exception:
        log.exception("could not mark stream %s dead", stream_id)


@router.api_route("/l/{sid}/{name}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_live(sid: str, name: str, request: Request, exp: str = "", sig: str = ""):
    cors = {**_cors(request.headers.get("origin")), "Cache-Control": "no-store"}
    if not SID.fullmatch(sid) or not NAME.fullmatch(name):
        return Response("not found", 404, cors, media_type="text/plain")
    if not exp.isdigit() or len(exp) > 12 or len(sig) != 64 or not verify("live:" + sid, exp, sig):
        return Response("invalid or expired link", 403, cors, media_type="text/plain")
    sess = registry.get(sid)
    if sess is None:                                         # server restarted, or the session was ended / expired
        return _respond(_error_reply(RelayError("live_session_gone", 404, "this live stream is gone: start it again (POST /v1/tasks/{id}/live)"), cors), request.method)
    reply = await serve(registry, sess, fetcher, name, f"exp={exp}&sig={sig}", request.method, request.headers.get("range"), cors,
                        safe=url_is_safe)
    if reply.mark_dead:
        await _mark_dead(sess.stream_id)
    return _respond(reply, request.method)


@router.options("/l/{sid}/{name}", include_in_schema=False)
async def live_preflight(sid: str, name: str, request: Request):
    return Response(status_code=204, headers=_cors(request.headers.get("origin")))


# ---------------------------------------------------------------- admin
@admin_router.get("/live")
async def admin_live():
    """Live streams being relayed right now (and recent ones): bandwidth, errors."""
    return registry.snapshot()


@admin_router.delete("/live/{live_id}", status_code=204)
async def admin_live_end(live_id: str):
    if not registry.end(live_id):
        raise HTTPException(404, "live stream not found")
