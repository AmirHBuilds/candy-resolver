from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_api_key
from ..db import get_db
from ..models import ApiKey, Task
from ..resolver import fetch_task, is_expired, peek, start_task, task_to_out
from ..schemas import ResolveRequest, TaskOut
from ..tmdb import TmdbError, TmdbNotFound, build_meta
from ..waiting import TERMINAL, wait_for_task

router = APIRouter(prefix="/v1", tags=["resolve"])

WAIT_DEFAULT_S = 10


@router.post("/resolve", response_model=TaskOut, status_code=202)
async def resolve(
    body: ResolveRequest,
    response: Response,
    wait: Literal["first", "first_starred", "all"] | None = Query(
        None, description="first = answer as soon as ONE source has streams (the rest keep running); "
                          "first_starred = same, but only counting starred (reliable) sources; "
                          "all = answer when every source is done"),
    wait_s: int = Query(0, ge=0, le=30, description="Longest time to wait (seconds). Default 10 when `wait` is set."),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Start a task. Without `wait` it returns at once; keep asking GET /v1/tasks/{id}?after=<version>&wait_s=25."""
    try:
        meta = await build_meta(body.type, body.tmdb_id, body.season, body.episode)
    except TmdbNotFound:
        raise HTTPException(404, "TMDB id not found")
    except TmdbError as e:
        raise HTTPException(502, str(e))

    task = Task(api_key_id=key.id, tmdb_id=body.tmdb_id, media_type=body.type,
                season=body.season, episode=body.episode, meta=meta, status="pending")
    db.add(task)
    await db.commit()
    task_id = task.id

    start_task(task_id)
    timeout = wait_s or (WAIT_DEFAULT_S if wait else 0)
    if timeout:
        await wait_for_task(peek, task_id, mode=wait or "all", timeout=timeout)

    out = task_to_out(await fetch_task(db, task_id))
    response.status_code = 200 if out.status in TERMINAL else 202
    return out


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: str,
    after: int | None = Query(None, ge=0, description="The `version` you last saw. With wait_s: hold the request until something new."),
    wait: Literal["first", "first_starred", "all"] | None = Query(None, description="Without `after`: wait for the first stream (any / starred only), or for everything"),
    wait_s: int = Query(0, ge=0, le=30, description="Longest time to hold the request (long polling)"),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    st = await peek(task_id)
    if st is None or st["api_key_id"] != key.id:
        raise HTTPException(404, "task not found")
    if st["expired"]:
        raise HTTPException(410, "task expired")

    if wait_s:
        mode = "change" if after is not None else (wait or "all")
        await wait_for_task(peek, task_id, mode=mode, after=after or 0, timeout=wait_s)

    task = await fetch_task(db, task_id)
    if task is None:                 # purged while we were waiting
        raise HTTPException(404, "task not found")
    if is_expired(task):
        raise HTTPException(410, "task expired")
    return task_to_out(task)
