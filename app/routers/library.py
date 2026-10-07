import shutil
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_api_key
from ..config import settings
from ..db import get_db
from ..downloader import cancel_download, start_download
from ..fileutil import is_hls
from ..library import item_to_out
from ..models import ApiKey, LibraryItem, SourceRun, Stream, Task, as_utc, utcnow
from ..resolver import is_expired
from ..schemas import LibraryItemOut, LibraryRequest

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
    """Download one stream of a task to our server. Poll GET /v1/library/{id} until status is 'ready'."""
    task = await db.get(Task, task_id)
    if task is None or task.api_key_id != key.id:
        raise HTTPException(404, "task not found")
    if is_expired(task):
        raise HTTPException(410, "task expired")

    ttl = body.ttl_hours or settings.library_ttl_hours
    if ttl > settings.library_max_ttl_hours:
        raise HTTPException(422, f"ttl_hours can be at most {settings.library_max_ttl_hours}")

    row = (await db.execute(
        select(Stream, SourceRun).join(SourceRun, Stream.run_id == SourceRun.id)
        .where(Stream.id == body.stream_id, SourceRun.task_id == task_id))).first()
    if row is None:
        raise HTTPException(404, "stream not found in this task")
    stream, run = row

    # For HLS the client may choose the quality; for plain files the stream's own quality label stands.
    quality = (body.quality or stream.quality) if is_hls(stream.format, stream.url) else stream.quality

    # Same stream (and quality) already queued / downloading / ready -> reuse it, extend its time if asked.
    same_q = LibraryItem.quality.is_(None) if quality is None else LibraryItem.quality == quality
    existing = (await db.execute(
        select(LibraryItem)
        .where(LibraryItem.stream_id == stream.id, LibraryItem.api_key_id == key.id, same_q,
               LibraryItem.status.in_(["queued", "downloading", "ready"]))
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
                       source_name=run.source_name, quality=quality, format=stream.format,
                       status="queued", ttl_hours=ttl)
    db.add(item)
    await db.commit()
    start_download(item.id, stream.url, stream.headers, stream.format)
    return item_to_out(item, _base(request))


@router.get("/library", response_model=list[LibraryItemOut])
async def list_library(request: Request, key: ApiKey = Depends(require_api_key),
                       db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(LibraryItem).where(LibraryItem.api_key_id == key.id)
                           .order_by(LibraryItem.requested_at.desc()).limit(100))
    return [item_to_out(i, _base(request)) for i in res.scalars()]


@router.get("/tasks/{task_id}/library", response_model=list[LibraryItemOut])
async def list_task_library(task_id: str, request: Request, key: ApiKey = Depends(require_api_key),
                            db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(LibraryItem).where(
        LibraryItem.api_key_id == key.id, LibraryItem.task_id == task_id)
        .order_by(LibraryItem.requested_at.desc()))
    return [item_to_out(i, _base(request)) for i in res.scalars()]


@router.get("/library/{item_id}", response_model=LibraryItemOut)
async def get_library_item(item_id: str, request: Request, key: ApiKey = Depends(require_api_key),
                           db: AsyncSession = Depends(get_db)):
    """Status + progress. When ready, `url` is a fresh signed link on our own domain."""
    return item_to_out(await _owned(db, item_id, key), _base(request))


@router.delete("/library/{item_id}", status_code=204)
async def delete_library_item(item_id: str, key: ApiKey = Depends(require_api_key),
                              db: AsyncSession = Depends(get_db)):
    item = await _owned(db, item_id, key)
    cancel_download(item.id)
    shutil.rmtree(settings.library_path / item.id, ignore_errors=True)
    item.status = "deleted"
    item.file_path = None
    await db.commit()
