import asyncio
import re
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audio import FORMATS, AudioError, AudioStream, build_cmd, probe_audio
from ..auth import require_api_key
from ..config import settings
from ..db import get_db
from ..downloader import cancel_download, start_download
from ..itemfiles import item_dir, remove_item_dir
from ..subs_job import build_plan, cancel_subtitles, default_lang, start_subtitles
from ..fileutil import is_hls
from ..library import item_to_out
from ..models import ApiKey, LibraryItem, SourceRun, Stream, Task, as_utc, public_label, utcnow
from ..resolver import is_expired
from ..schemas import LibraryItemOut, LibraryRequest, TaskSubtitlesOut, TaskSubtitlesRequest

router = APIRouter(prefix="/v1", tags=["library"])


def _base(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


async def _owned(db: AsyncSession, item_id: str, key: ApiKey) -> LibraryItem:
    item = await db.get(LibraryItem, item_id)
    if item is None or item.api_key_id != key.id:
        raise HTTPException(404, "library item not found")
    return item


@router.post("/tasks/{task_id}/library", response_model=LibraryItemOut, status_code=202)
async def request_library(task_id: str, body: LibraryRequest, request: Request, response: Response,
                          key: ApiKey = Depends(require_api_key), db: AsyncSession = Depends(get_db)):
    """Download one stream of a task to our server. Poll GET /v1/library/{id} until status is 'ready'.
    With "progressive": true it is packaged as HLS and `stream_url` is playable while the download still runs."""
    task = await db.get(Task, task_id)
    if task is None or task.api_key_id != key.id:
        raise HTTPException(404, "task not found")
    if is_expired(task):
        raise HTTPException(410, "this task's results have expired (the links are stale) - resolve again to download")

    ttl = body.ttl_hours or settings.library_ttl_hours
    if ttl > settings.library_max_ttl_hours:
        raise HTTPException(422, f"ttl_hours can be at most {settings.library_max_ttl_hours}")

    row = (await db.execute(
        select(Stream, SourceRun).join(SourceRun, Stream.run_id == SourceRun.id)
        .where(Stream.id == body.stream_id, SourceRun.task_id == task_id))).first()
    if row is None:
        raise HTTPException(404, "stream not found in this task")
    stream, run = row
    if stream.dead or (stream.expires_at and as_utc(stream.expires_at) <= utcnow()):
        raise HTTPException(410, {"code": "source_link_expired", "message": "this stream link has stopped working: pick another "
                                  "stream or source from the task, or ask for more sources (POST /v1/tasks/{id}/sources), or resolve again"})

    # For HLS the client may choose the quality; for plain files the stream's own quality label stands.
    quality = (body.quality or stream.quality) if is_hls(stream.format, stream.url) else stream.quality

    mode = "hls" if body.progressive else "file"
    # Subtitles of the SAME source run as this stream. Options only matter for a new item: asking again for the
    # same stream reuses that item and the subtitles it already has (no second download).
    sub_plan = build_plan(run.subtitles, body.subtitles, body.subtitle_langs)
    # Two requests are "the same download" only if everything that shapes the result matches: stream, quality, mode
    # (progressive or not) AND the subtitle options. ttl_hours is not part of it: reusing just extends the time.
    langs = "*" if body.subtitle_langs is None else ",".join(sorted(x.lower() for x in body.subtitle_langs))
    opts = f"sub={int(bool(sub_plan))};langs={langs};default={(body.subtitle_default or '').lower()}"[:160]

    # Same stream (and quality, and mode) already queued / downloading / ready -> reuse it, extend its time if asked.
    same_q = LibraryItem.quality.is_(None) if quality is None else LibraryItem.quality == quality
    existing = (await db.execute(
        select(LibraryItem)
        .where(LibraryItem.stream_id == stream.id, LibraryItem.api_key_id == key.id, same_q, LibraryItem.mode == mode,
               LibraryItem.opts == opts, LibraryItem.status.in_(["queued", "downloading", "ready"]))
        .order_by(LibraryItem.requested_at.desc()))).scalars().first()
    if existing and existing.status == "ready" and as_utc(existing.delete_at) <= utcnow():
        existing = None  # time is up, cleanup just hasn't run yet
    if existing:
        existing.ttl_hours = max(existing.ttl_hours, ttl)
        if existing.status == "ready":
            wanted = utcnow() + timedelta(hours=ttl)
            if as_utc(existing.delete_at) < wanted:
                existing.delete_at = wanted
            response.status_code = 200
        await db.commit()
        return item_to_out(existing, _base(request))

    item = LibraryItem(api_key_id=key.id, task_id=task_id, stream_id=stream.id,
                       source_name=run.source_name, public_name=run.public_name or public_label(None, run.source_id),
                       quality=quality, format=stream.format, mode=mode,
                       tmdb_id=task.tmdb_id, media_type=task.media_type, season=task.season, episode=task.episode,
                       title=(task.meta or {}).get("title"),
                       status="queued", ttl_hours=ttl, opts=opts, subtitles=[], subtitles_status="pending" if sub_plan else "none")
    db.add(item)
    await db.commit()
    item_dir(item.id).mkdir(parents=True, exist_ok=True)      # exists before either job starts (subtitles never create it)
    start_download(item.id, stream.url, stream.headers, stream.format)
    if sub_plan:                                              # in parallel with the video, never after it
        start_subtitles(item.id, sub_plan, stream.headers, default_lang(body.subtitle_default))
    return item_to_out(item, _base(request))


@router.get("/library", response_model=list[LibraryItemOut])
async def list_library(
    request: Request,
    tmdb_id: int | None = Query(None),
    media_type: Literal["movie", "tv"] | None = Query(None, alias="type"),
    season: int | None = Query(None, ge=0),
    episode: int | None = Query(None, ge=1),
    status: str | None = Query(None),
    mode: Literal["file", "hls", "subs"] | None = Query(None, description="default: file + hls (not subtitle packs)"),
    limit: int = Query(100, ge=1, le=200),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Your library items, newest first. Filter by movie/episode, e.g. ?tmdb_id=26573&type=movie."""
    q = select(LibraryItem).where(LibraryItem.api_key_id == key.id)
    q = q.where(LibraryItem.mode == mode) if mode else q.where(LibraryItem.mode != "subs")
    if tmdb_id is not None:
        q = q.where(LibraryItem.tmdb_id == tmdb_id)
    if media_type is not None:
        q = q.where(LibraryItem.media_type == media_type)
    if season is not None:
        q = q.where(LibraryItem.season == season)
    if episode is not None:
        q = q.where(LibraryItem.episode == episode)
    if status:
        q = q.where(LibraryItem.status == status)
    res = await db.execute(q.order_by(LibraryItem.requested_at.desc()).limit(limit))
    return [item_to_out(i, _base(request)) for i in res.scalars()]


@router.get("/tasks/{task_id}/library", response_model=list[LibraryItemOut])
async def list_task_library(task_id: str, request: Request, key: ApiKey = Depends(require_api_key),
                            db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(LibraryItem).where(
        LibraryItem.api_key_id == key.id, LibraryItem.task_id == task_id, LibraryItem.mode != "subs")
        .order_by(LibraryItem.requested_at.desc()))
    return [item_to_out(i, _base(request)) for i in res.scalars()]


@router.get("/library/{item_id}", response_model=LibraryItemOut)
async def get_library_item(item_id: str, request: Request, key: ApiKey = Depends(require_api_key),
                           db: AsyncSession = Depends(get_db)):
    """Status + progress. When ready, `url` is a fresh signed link on our own domain."""
    return item_to_out(await _owned(db, item_id, key), _base(request))


_audio_sem: asyncio.Semaphore | None = None


@router.get("/library/{item_id}/audio", responses={200: {"content": {"audio/aac": {}, "audio/mpeg": {}, "audio/ogg": {}}}})
async def get_library_audio(
    item_id: str,
    format: Literal["aac", "mp3", "opus"] = Query("aac", description="aac (default; copied without re-encoding when the video's audio is already AAC), mp3 or opus"),
    track: int = Query(0, ge=0, le=31, description="Which audio track, 0 = the first"),
    start: float | None = Query(None, ge=0, description="Start at this second"),
    duration: float | None = Query(None, gt=0, description="Only this many seconds"),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Only the AUDIO of a downloaded library item, streamed as it is produced (no video, nothing stored). Use the same
    API key as for the rest of /v1. The item must be `ready` (file or progressive HLS). Not seekable: no Range support."""
    global _audio_sem
    item = await _owned(db, item_id, key)
    if item.mode == "subs":
        raise HTTPException(404, "library item not found")
    if item.status in ("expired", "deleted") or (item.status == "ready" and item.delete_at and as_utc(item.delete_at) <= utcnow()):
        raise HTTPException(410, "this file is gone")
    if item.status != "ready" or not item.file_path:
        raise HTTPException(409, f"the download is not finished (status: {item.status})")
    path = Path(item.file_path)
    if not path.is_file():
        raise HTTPException(410, "this file is gone")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", item.title or item.id).strip("_")[:80] or item.id
    if item.season is not None and item.episode is not None:
        name += f"_S{item.season:02d}E{item.episode:02d}"
    await db.close()                                  # do not hold a database connection while streaming

    if _audio_sem is None:
        _audio_sem = asyncio.Semaphore(max(1, settings.audio_concurrency))
    sem = _audio_sem
    if sem.locked():
        raise HTTPException(429, "too many audio extractions running: try again in a few seconds", headers={"Retry-After": "5"})
    await sem.acquire()
    state = {"stream": None, "released": False}

    def release() -> None:                           # safe to call twice (generator end AND response end)
        if state["stream"] is not None:
            state["stream"].close()
        if not state["released"]:
            state["released"] = True
            sem.release()

    try:
        codecs, total = await probe_audio(settings.ffprobe_path, path)
        if not codecs:
            raise AudioError("this video has no audio track", 422)
        if track >= len(codecs):
            raise AudioError(f"audio track {track} does not exist (this file has {len(codecs)})", 422)
        if start and total and start >= total:
            raise AudioError("start is after the end of the video", 422)
        stream = AudioStream(build_cmd(settings.ffmpeg_path, path, format, track, codecs[track], start, duration))
        state["stream"] = stream
        await stream.start()
    except AudioError as e:
        release()
        raise HTTPException(e.status, str(e))
    except BaseException:
        release()
        raise

    async def body():
        try:
            async for chunk in stream.chunks():
                yield chunk
        finally:
            release()

    ctype, ext, _, _ = FORMATS[format]
    return StreamingResponse(body(), media_type=ctype, background=BackgroundTask(release), headers={
        "Content-Disposition": f'attachment; filename="{name}.{ext}"', "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"})


@router.delete("/library/{item_id}", status_code=204)
async def delete_library_item(item_id: str, key: ApiKey = Depends(require_api_key),
                              db: AsyncSession = Depends(get_db)):
    item = await _owned(db, item_id, key)
    cancel_download(item.id)
    cancel_subtitles(item.id)
    remove_item_dir(item.id)
    item.status = "deleted"
    item.file_path = None
    item.playable = False
    item.subtitles = []
    item.subtitles_status = "none"
    await db.commit()


# ---------------------------------------------------------------- subtitles of a whole task (all sources)
_pack_lock = asyncio.Lock()


async def _live_pack(db: AsyncSession, task_id: str, key_id: int) -> LibraryItem | None:
    res = await db.execute(select(LibraryItem).where(
        LibraryItem.task_id == task_id, LibraryItem.api_key_id == key_id, LibraryItem.mode == "subs",
        LibraryItem.status.in_(["downloading", "ready"])).order_by(LibraryItem.requested_at.desc()))
    for it in res.scalars():
        if it.status == "ready" and (it.delete_at is None or as_utc(it.delete_at) <= utcnow()):
            continue                         # time is up, cleanup just has not run yet
        return it
    return None


def _pack_out(item: LibraryItem, base: str) -> TaskSubtitlesOut:
    o = item_to_out(item, base)
    return TaskSubtitlesOut(id=o.id, task_id=o.task_id, tmdb_id=item.tmdb_id, type=item.media_type, season=item.season,
                            episode=item.episode, title=item.title, status=o.subtitles_status, subtitles=o.subtitles,
                            ttl_hours=o.ttl_hours, delete_at=o.delete_at)


@router.post("/tasks/{task_id}/subtitles", response_model=TaskSubtitlesOut, status_code=202)
async def host_task_subtitles(task_id: str, body: TaskSubtitlesRequest, request: Request, response: Response,
                              key: ApiKey = Depends(require_api_key), db: AsyncSession = Depends(get_db)):
    """Download + convert + host the subtitles of EVERY source of this task (no video needed), starred sources first.
    Each track says which provider it came from (public name). Poll GET /v1/tasks/{id}/subtitles until status is not 'pending'."""
    task = await db.get(Task, task_id)
    if task is None or task.api_key_id != key.id:
        raise HTTPException(404, "task not found")
    base = _base(request)
    async with _pack_lock:                       # two quick calls must not start two downloads
        existing = await _live_pack(db, task_id, key.id)
        if existing:
            response.status_code = 200
            return _pack_out(existing, base)
        if is_expired(task):
            raise HTTPException(410, "this task's results have expired (the subtitle links are stale) - resolve again")
        ttl = body.ttl_hours or settings.subtitle_pack_ttl_hours
        if ttl > settings.library_max_ttl_hours:
            raise HTTPException(422, f"ttl_hours can be at most {settings.library_max_ttl_hours}")

        runs = (await db.execute(select(SourceRun).where(SourceRun.task_id == task_id).order_by(SourceRun.id))).scalars().all()
        first_headers: dict = {}
        rows = (await db.execute(select(Stream.run_id, Stream.headers).join(SourceRun, Stream.run_id == SourceRun.id)
                                 .where(SourceRun.task_id == task_id).order_by(Stream.id))).all()
        for run_id, headers in rows:             # a subtitle without its own headers uses its run's stream headers
            if headers and run_id not in first_headers:
                first_headers[run_id] = headers
        raw = []
        for r in sorted(runs, key=lambda r: (not r.starred, r.id)):     # reliable (starred) sources first
            for sub in (r.subtitles or []):
                if isinstance(sub, dict):
                    d = dict(sub)
                    d["_source"] = r.public_name or public_label(None, r.source_id)
                    if not isinstance(d.get("headers"), dict) and r.id in first_headers:
                        d["headers"] = first_headers[r.id]
                    raw.append(d)
        plan = build_plan(raw, True, body.subtitle_langs)

        now = utcnow()
        item = LibraryItem(
            api_key_id=key.id, task_id=task_id, stream_id=f"pack:{task_id}", source_name="all sources",
            public_name="All sources", mode="subs", tmdb_id=task.tmdb_id, media_type=task.media_type,
            season=task.season, episode=task.episode, title=(task.meta or {}).get("title"), ttl_hours=ttl,
            status="downloading" if plan else "ready", subtitles=[], subtitles_status="pending" if plan else "none",
            ready_at=None if plan else now, delete_at=None if plan else now + timedelta(hours=ttl), size=0)
        db.add(item)
        await db.commit()
        if plan:
            item_dir(item.id).mkdir(parents=True, exist_ok=True)
            start_subtitles(item.id, plan, None, default_lang(body.subtitle_default), pack=True)
        return _pack_out(item, base)


@router.get("/tasks/{task_id}/subtitles", response_model=TaskSubtitlesOut)
async def get_task_subtitles(task_id: str, request: Request, key: ApiKey = Depends(require_api_key),
                             db: AsyncSession = Depends(get_db)):
    """The hosted subtitles of a task (all sources). Works after the task's results expired, until they are deleted."""
    item = await _live_pack(db, task_id, key.id)
    if item is None:
        raise HTTPException(404, "no hosted subtitles for this task (or they were deleted) - POST /v1/tasks/{id}/subtitles")
    return _pack_out(item, _base(request))
