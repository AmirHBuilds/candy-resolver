import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_api_key
from ..db import get_db
from ..models import ApiKey, Task
from ..resolver import fetch_task, is_expired, start_task, task_to_out
from ..schemas import ResolveRequest, TaskOut
from ..tmdb import TmdbError, TmdbNotFound, build_meta

router = APIRouter(prefix="/v1", tags=["resolve"])


@router.post("/resolve", response_model=TaskOut, status_code=202)
async def resolve(
    body: ResolveRequest,
    wait_s: int = Query(0, ge=0, le=30, description="Wait up to N seconds for results before returning"),
    key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
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

    bg = start_task(task.id)
    if wait_s:
        try:
            await asyncio.wait_for(asyncio.shield(bg), wait_s)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass  # failure is recorded on the task itself

    return task_to_out(await fetch_task(db, task.id))


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, key: ApiKey = Depends(require_api_key),
                   db: AsyncSession = Depends(get_db)):
    task = await fetch_task(db, task_id)
    if task is None or task.api_key_id != key.id:
        raise HTTPException(404, "task not found")
    if is_expired(task):
        raise HTTPException(410, "task expired")
    return task_to_out(task)
