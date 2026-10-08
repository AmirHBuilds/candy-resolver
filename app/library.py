import shutil
from datetime import datetime, timezone

from sqlalchemy import select, update

from .config import settings
from .db import SessionLocal
from .models import LibraryItem, as_utc, public_label, utcnow
from .schemas import LibraryItemOut
from .signing import sign


def item_to_out(item: LibraryItem, base_url: str) -> LibraryItemOut:
    url = url_exp = None
    now = utcnow()
    if item.status == "ready" and item.file_path and item.delete_at and as_utc(item.delete_at) > now:
        remaining = int((as_utc(item.delete_at) - now).total_seconds())
        exp, sig = sign(item.id, min(settings.signed_url_ttl_min * 60, remaining))
        filename = item.file_path.rsplit("/", 1)[-1]
        url = f"{base_url}/f/{item.id}/{filename}?exp={exp}&sig={sig}"
        url_exp = datetime.fromtimestamp(exp, timezone.utc)
    if item.total_bytes:
        progress = round(min((item.size or 0) / item.total_bytes, 1.0), 4)
    else:
        progress = 1.0 if item.status == "ready" else None
    return LibraryItemOut(
        id=item.id, task_id=item.task_id, stream_id=item.stream_id, source=item.public_name or public_label(None),
        quality=item.quality, format=item.format, status=item.status, error=item.error,
        progress=progress, size=item.size, total_bytes=item.total_bytes, ttl_hours=item.ttl_hours,
        requested_at=item.requested_at, ready_at=item.ready_at, delete_at=item.delete_at,
        url=url, url_expires_at=url_exp,
    )


async def expire_library() -> int:
    """Delete files whose time is up (runs every few minutes)."""
    async with SessionLocal() as db:
        res = await db.execute(select(LibraryItem).where(
            LibraryItem.status == "ready", LibraryItem.delete_at < utcnow()))
        items = res.scalars().all()
        for it in items:
            shutil.rmtree(settings.library_path / it.id, ignore_errors=True)
            it.status = "expired"
            it.file_path = None
        await db.commit()
        return len(items)


async def fail_stuck_library() -> None:
    """Downloads that were running when the server stopped cannot resume."""
    async with SessionLocal() as db:
        await db.execute(
            update(LibraryItem)
            .where(LibraryItem.status.in_(["queued", "downloading"]))
            .values(status="failed", error="server restarted during download"))
        await db.commit()


async def sweep_orphans() -> None:
    """Remove library folders that no live item points to (crashes, partial files)."""
    base = settings.library_path
    base.mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as db:
        res = await db.execute(select(LibraryItem.id).where(
            LibraryItem.status.in_(["queued", "downloading", "ready"])))
        keep = set(res.scalars())
    for child in base.iterdir():
        if child.is_dir() and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)
