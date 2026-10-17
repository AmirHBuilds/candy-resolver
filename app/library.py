import shutil
from datetime import datetime, timezone

from sqlalchemy import select, update

from . import speedmeter
from .config import settings
from .db import SessionLocal
from .models import LibraryItem, Task, as_utc, public_label, utcnow
from .schemas import LibraryItemOut, SubtitleOut
from .itemfiles import remove_item_dir
from .subs_job import cancel_subtitles
from .signing import sign


def fmt_duration(seconds: float | None) -> str | None:
    """90 -> \"1:30\", 5370 -> \"1:29:30\"."""
    if not seconds or seconds <= 0:
        return None
    h, rem = divmod(int(round(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def item_to_out(item: LibraryItem, base_url: str) -> LibraryItemOut:
    """The one place that turns a library row into API output (used by every library endpoint)."""
    now = utcnow()
    mode = item.mode or "file"
    remaining = int((as_utc(item.delete_at) - now).total_seconds()) if item.delete_at else None
    url = url_exp = stream_url = stream_exp = None

    if mode == "hls":
        alive = item.status == "downloading" or (item.status == "ready" and remaining is not None and remaining > 0)
        if item.playable and alive:
            ttl = settings.hls_link_ttl_min * 60          # long enough for a whole movie
            if item.status == "ready":
                ttl = min(ttl, remaining)                  # never outlive the file
            exp, sig = sign(item.id, ttl)
            stream_url = f"{base_url}/h/{item.id}/index.m3u8?exp={exp}&sig={sig}"
            stream_exp = datetime.fromtimestamp(exp, timezone.utc)
        playable = stream_url is not None
    else:
        if item.status == "ready" and item.file_path and remaining is not None and remaining > 0:
            exp, sig = sign(item.id, min(settings.signed_url_ttl_min * 60, remaining))
            filename = item.file_path.rsplit("/", 1)[-1]
            url = f"{base_url}/f/{item.id}/{filename}?exp={exp}&sig={sig}"
            url_exp = datetime.fromtimestamp(exp, timezone.utc)
        playable = url is not None

    subs, sub_status = [], "none"
    if item.status in ("queued", "downloading") or (item.status == "ready" and remaining is not None and remaining > 0):
        sub_status = item.subtitles_status or "none"
        if item.subtitles:
            ttl = (settings.hls_link_ttl_min if mode == "hls" else settings.signed_url_ttl_min) * 60
            if item.status == "ready":
                ttl = min(ttl, remaining)
            s_exp, s_sig = sign(item.id, ttl)
            subs = [SubtitleOut(lang=t["lang"], label=t["label"], format="vtt", default=bool(t.get("default")), source=t.get("source"),
                                url=f"{base_url}/s/{item.id}/{t['n']}.vtt?exp={s_exp}&sig={s_sig}") for t in item.subtitles]

    if item.status in ("ready", "expired"):
        progress = 1.0
    elif item.total_bytes:
        progress = round(min((item.size or 0) / item.total_bytes, 0.99), 4)   # 1.0 only once really ready
    else:
        progress = None

    speed, eta = speedmeter.read(item.id) if item.status == "downloading" else (None, None)
    return LibraryItemOut(
        id=item.id, task_id=item.task_id, stream_id=item.stream_id, source=item.public_name or public_label(None),
        quality=item.quality, format=item.format, status=item.status, error=item.error,
        progress=progress, size=item.size, total_bytes=item.total_bytes,
        duration_s=item.duration_s, duration=fmt_duration(item.duration_s),
        error_code="source_link_expired" if (item.error or "").startswith("source_link_expired") else None, ttl_hours=item.ttl_hours,
        requested_at=item.requested_at, ready_at=item.ready_at, delete_at=item.delete_at,
        url=url, url_expires_at=url_exp, mode=mode, playable=playable,
        stream_url=stream_url, stream_url_expires_at=stream_exp, download_speed=speed, eta_seconds=eta,
        subtitles=subs, subtitles_status=sub_status,
        tmdb_id=item.tmdb_id, type=item.media_type, season=item.season, episode=item.episode, title=item.title,
    )


async def expire_library() -> int:
    """Delete files whose time is up (runs every few minutes). Works for file and HLS items: the whole folder goes."""
    async with SessionLocal() as db:
        res = await db.execute(select(LibraryItem).where(
            LibraryItem.status == "ready", LibraryItem.delete_at < utcnow()))
        items = res.scalars().all()
        for it in items:
            cancel_subtitles(it.id)
            remove_item_dir(it.id)                       # video, playlist, segments and subs/ together
            it.status = "expired"
            it.file_path = None
            it.playable = False
            it.subtitles = []
            it.subtitles_status = "none"
        await db.commit()
        return len(items)


async def fail_stuck_library() -> None:
    """Downloads that were running when the server stopped cannot resume (their folders are swept next)."""
    async with SessionLocal() as db:
        await db.execute(
            update(LibraryItem)
            .where(LibraryItem.status.in_(["queued", "downloading"]))
            .values(status="failed", error="server restarted during download", playable=False))
        res = await db.execute(select(LibraryItem).where(LibraryItem.subtitles_status == "pending"))
        for it in res.scalars():                         # no subtitle job survives a restart: make "pending" honest
            it.subtitles_status = "partial" if it.subtitles else "none"
        await db.commit()


async def sweep_orphans() -> None:
    """Remove library folders that no live item points to (crashes, partial files, half-written HLS)."""
    base = settings.library_path
    base.mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as db:
        res = await db.execute(select(LibraryItem.id).where(
            LibraryItem.status.in_(["queued", "downloading", "ready"])))
        keep = set(res.scalars())
    for child in base.iterdir():
        if child.is_dir() and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)


async def backfill_library_info() -> None:
    """One-time on startup: items created before movie info was stored get it from their task, if it still exists."""
    async with SessionLocal() as db:
        res = await db.execute(select(LibraryItem, Task).join(Task, Task.id == LibraryItem.task_id)
                               .where(LibraryItem.tmdb_id.is_(None)).limit(2000))
        for it, t in res.all():
            it.tmdb_id, it.media_type, it.season, it.episode = t.tmdb_id, t.media_type, t.season, t.episode
            it.title = (t.meta or {}).get("title")
        await db.commit()
