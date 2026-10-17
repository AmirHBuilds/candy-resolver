import asyncio
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_api_key
from ..config import settings
from ..db import get_db
from ..library import item_to_out
from ..models import ApiKey, LibraryItem, Script, Source, SourceRun, Task, as_utc, public_label, utcnow
from ..resolver import fetch_task, peek, running, start_task, task_summary, task_to_out
from ..scheduler import queue
from ..schemas import MoreSourcesRequest, RefreshRequest, ResolveRequest, TaskOut, TaskSummary
from ..refresh import select_runs
from ..taskstate import effective_status, stream_alive
from ..tmdb import TmdbError, TmdbNotFound, build_meta
from ..waiting import TERMINAL, wait_for_task

router = APIRouter(prefix="/v1", tags=["resolve"])

WAIT_DEFAULT_S = 10
IN_FLIGHT = ("pending", "queued", "running")
_create_lock = asyncio.Lock()       # two identical requests arriving together must end up sharing ONE task


def _base(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


async def _with_library(out: TaskOut, db: AsyncSession, key_id: int, request: Request) -> TaskOut:
    """Attach the task's library items (video downloads), newest first. They outlive the task's results."""
    res = await db.execute(select(LibraryItem).where(
        LibraryItem.api_key_id == key_id, LibraryItem.task_id == out.task_id, LibraryItem.mode != "subs")
        .order_by(LibraryItem.requested_at.desc()))
    out.library = [item_to_out(i, _base(request)) for i in res.scalars()]
    return out


@router.post("/resolve", response_model=TaskOut, status_code=202)
async def resolve(
    body: ResolveRequest,
    request: Request,
    response: Response,
    wait: Literal["first", "first_starred", "all"] | None = Query(
        None, description="first = answer as soon as ONE source has streams (the rest keep running); "
                          "first_starred = same, but only counting starred (reliable) sources; "
                          "all = answer when every source is done"),
    wait_s: int = Query(0, ge=0, le=30, description="Longest time to wait (seconds). Default 10 when `wait` is set."),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Start a task. Without `wait` it returns at once; keep asking GET /v1/tasks/{id}?after=<version>&wait_s=25.
    `max_sources` limits how many sources are asked (best rank first); POST /v1/tasks/{id}/sources asks for more later."""
    async with _create_lock:
        # The very same request (same movie / episode, same options, same API key) is already waiting or running: hand that task
        # back instead of starting a second one (each run costs real memory). Finished tasks are not reused.
        same_season = Task.season.is_(None) if body.season is None else Task.season == body.season
        same_episode = Task.episode.is_(None) if body.episode is None else Task.episode == body.episode
        same_wanted = Task.sources_wanted.is_(None) if body.max_sources is None else Task.sources_wanted == body.max_sources
        dup = (await db.execute(
            select(Task).where(Task.api_key_id == key.id, Task.tmdb_id == body.tmdb_id, Task.media_type == body.type,
                               same_season, same_episode, Task.starred_first == body.starred_first, same_wanted,
                               Task.status.in_(IN_FLIGHT))
            .order_by(Task.created_at.desc()))).scalars().first()
        if dup is not None and dup.id in running:
            task_id = dup.id
        else:
            if queue.full():
                raise HTTPException(429, f"the server is busy ({settings.max_queued_tasks} requests are already waiting): "
                                         "try again in a few seconds", headers={"Retry-After": "10"})
            try:
                meta = await build_meta(body.type, body.tmdb_id, body.season, body.episode)
            except TmdbNotFound:
                raise HTTPException(404, "TMDB id not found")
            except TmdbError as e:
                raise HTTPException(502, str(e))
            task = Task(api_key_id=key.id, tmdb_id=body.tmdb_id, media_type=body.type,
                        season=body.season, episode=body.episode, starred_first=body.starred_first,
                        sources_wanted=body.max_sources, meta=meta, status="pending")
            db.add(task)
            await db.commit()
            task_id = task.id
            start_task(task_id)

    timeout = wait_s or (WAIT_DEFAULT_S if wait else 0)
    if timeout:
        await wait_for_task(peek, task_id, mode=wait or "all", timeout=timeout)

    out = task_to_out(await fetch_task(db, task_id))
    response.status_code = 200 if out.status in TERMINAL else 202
    return await _with_library(out, db, key.id, request)


@router.post("/tasks/{task_id}/sources", response_model=TaskOut, status_code=202)
async def more_sources(
    task_id: str,
    body: MoreSourcesRequest,
    request: Request,
    response: Response,
    wait: Literal["first", "all"] | None = Query(
        None, description="first = answer as soon as ONE of the new sources has streams; all = when the search is done"),
    wait_s: int = Query(0, ge=0, le=30, description="Longest time to wait (seconds). Default 10 when `wait` is set."),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Find `count` MORE sources for a finished task: the next ones in rank order that were not asked yet. The task goes
    back to \"running\" and keeps its id, so poll it as usual. `has_more` in the task says whether sources are left;
    if none are left the task is returned unchanged (200). The stream links share the task's expiry."""
    async with _create_lock:                      # two quick calls must not start two searches
        task = await fetch_task(db, task_id)
        if task is None or task.api_key_id != key.id:
            raise HTTPException(404, "task not found")
        status = effective_status(task.status, task.expires_at)
        if status == "expired":
            raise HTTPException(410, "this task's results have expired - resolve again")
        if status in IN_FLIGHT:
            raise HTTPException(409, "this task is still running: wait until its status is done or failed, then ask for more sources")
        tried = {r.source_id for r in task.runs if r.source_id is not None}
        left = (await db.execute(
            select(func.count()).select_from(Source).join(Script, Script.source_id == Source.id)
            .where(Source.enabled.is_(True), Script.active.is_(True), Source.id.not_in(list(tried))))).scalar_one()
        found = sum(1 for r in task.runs if r.status == "ok")
        started = left > 0
        if started:
            task.sources_wanted = found + body.count
            task.status = "pending"
            task.version = (task.version or 0) + 1
            await db.commit()
            start_task(task_id)

    timeout = wait_s or (WAIT_DEFAULT_S if wait else 0)
    if started and timeout:
        await wait_for_task(peek, task_id, mode="found" if wait == "first" else "all", after=found, timeout=timeout)

    out = task_to_out(await fetch_task(db, task_id))
    response.status_code = 200 if out.status in TERMINAL else 202
    return await _with_library(out, db, key.id, request)


@router.post("/tasks/{task_id}/refresh", response_model=TaskOut, status_code=202)
async def refresh_sources(
    task_id: str,
    body: RefreshRequest,
    request: Request,
    response: Response,
    wait: Literal["first", "all"] | None = Query(
        None, description="first = answer as soon as ONE refreshed source has streams; all = when every refreshed source is done"),
    wait_s: int = Query(0, ge=0, le=30, description="Longest time to wait (seconds). Default 10 when `wait` is set."),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Look up again ONLY the given sources of a finished task and replace their results (new stream links, new stream ids).
    The other sources are left exactly as they are. Name them in `sources` (as shown in the task's `sources[].source`), and/or
    ask for `expired` (every source whose links stopped working) or `failed` (every source whose run failed). The task goes back
    to "running" with its id unchanged; poll it as usual. Its expiry is renewed. 409 while the task is still running, 410 once its
    results expired, 422 for a name that is not in the task or a source that was disabled / removed meanwhile."""
    if not (body.sources or body.expired or body.failed):
        raise HTTPException(422, "say what to refresh: `sources` (names), `expired` or `failed`")
    async with _create_lock:
        task = await fetch_task(db, task_id)
        if task is None or task.api_key_id != key.id:
            raise HTTPException(404, "task not found")
        status = effective_status(task.status, task.expires_at)
        if status == "expired":
            raise HTTPException(410, "this task's results have expired - resolve again")
        if status in IN_FLIGHT:
            raise HTTPException(409, "this task is still running: wait until its status is done or failed, then refresh")
        now = utcnow()
        runs = [{"id": r.id, "label": r.public_name or public_label(None, r.source_id), "source_id": r.source_id, "status": r.status,
                 "streams": [(as_utc(s.expires_at) if s.expires_at else None, bool(s.dead)) for s in r.streams]} for r in task.runs]
        chosen, unknown = select_runs(runs, body.sources, body.expired, body.failed, now)
        if unknown:
            raise HTTPException(422, f"not a source of this task: {', '.join(unknown)}. Its sources: {', '.join(sorted({r['label'] for r in runs})) or 'none'}")
        started = bool(chosen)
        if started:
            ids = {c["source_id"] for c in chosen if c["source_id"] is not None}
            runnable = set((await db.execute(
                select(Source.id).join(Script, Script.source_id == Source.id)
                .where(Source.enabled.is_(True), Script.active.is_(True), Source.id.in_(list(ids))))).scalars()) if ids else set()
            stuck = sorted({c["label"] for c in chosen if c["source_id"] not in runnable})
            if stuck:
                raise HTTPException(422, f"cannot refresh {', '.join(stuck)}: the source is disabled, has no active script, or was removed")
            gone = {c["id"] for c in chosen}
            base_ok = sum(1 for r in task.runs if r.id not in gone and r.status == "ok")
            for r in task.runs:
                if r.id in gone:
                    await db.delete(r)                       # its streams go with it (cascade)
            task.status = "pending"
            task.version = (task.version or 0) + 1
            task.expires_at = max(as_utc(task.expires_at), now + timedelta(hours=settings.task_ttl_hours))
            await db.commit()
            start_task(task_id, only=ids)

    timeout = wait_s or (WAIT_DEFAULT_S if wait else 0)
    if started and timeout:
        await wait_for_task(peek, task_id, mode="found" if wait == "first" else "all", after=base_ok, timeout=timeout)

    out = task_to_out(await fetch_task(db, task_id))
    response.status_code = 200 if out.status in TERMINAL else 202
    return await _with_library(out, db, key.id, request)


@router.get("/tasks", response_model=list[TaskSummary])
async def list_tasks(
    tmdb_id: int | None = Query(None),
    media_type: Literal["movie", "tv"] | None = Query(None, alias="type"),
    season: int | None = Query(None, ge=0),
    episode: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Your tasks, newest first. Tasks are kept long after their results expire (status "expired")."""
    q = (select(Task, func.count(SourceRun.id)).outerjoin(SourceRun, SourceRun.task_id == Task.id)
         .where(Task.api_key_id == key.id))
    if tmdb_id is not None:
        q = q.where(Task.tmdb_id == tmdb_id)
    if media_type is not None:
        q = q.where(Task.media_type == media_type)
    if season is not None:
        q = q.where(Task.season == season)
    if episode is not None:
        q = q.where(Task.episode == episode)
    rows = (await db.execute(q.group_by(Task.id).order_by(Task.created_at.desc()).limit(limit))).all()
    return [task_summary(t, n) for t, n in rows]


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: str,
    request: Request,
    after: int | None = Query(None, ge=0, description="The `version` you last saw. With wait_s: hold the request until something new."),
    wait: Literal["first", "first_starred", "all"] | None = Query(None, description="Without `after`: wait for the first stream (any / starred only), or for everything"),
    wait_s: int = Query(0, ge=0, le=30, description="Longest time to hold the request (long polling)"),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    st = await peek(task_id)
    if st is None or st["api_key_id"] != key.id:
        raise HTTPException(404, "task not found")

    if wait_s:
        mode = "change" if after is not None else (wait or "all")
        await wait_for_task(peek, task_id, mode=mode, after=after or 0, timeout=wait_s)

    task = await fetch_task(db, task_id)
    if task is None:                 # removed while we were waiting
        raise HTTPException(404, "task not found")
    return await _with_library(task_to_out(task), db, key.id, request)   # expired tasks are still returned
