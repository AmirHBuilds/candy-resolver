"""Glue: runs the subtitle engine for a library item in the background, next to (never inside) the video download.
Whatever happens here can only ever mean "a subtitle is missing": it cannot fail, slow down or cancel the video."""
import asyncio
import traceback
from datetime import timedelta

from sqlalchemy import select, update

from .config import settings
from .db import SessionLocal
from .itemfiles import item_dir
from .models import LibraryItem, utcnow
from .subengine import Params, Plan, parse_langs, plan_subtitles, run_subtitles
from .subtitles import normalize_lang

jobs: dict[str, asyncio.Task] = {}
_sem: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _sem                                 # one shared limit for all items: protects a weak VPS
    if _sem is None:
        _sem = asyncio.Semaphore(settings.subtitle_concurrency)
    return _sem


def _allow_list(request_langs, setting: str) -> list[str] | None:
    """The request wins over the setting. None = every language. [] after parsing a non-empty list = nothing matches."""
    if request_langs is not None:
        wanted = parse_langs(request_langs)
        return wanted if (wanted or not request_langs) else ["-"]      # asked for languages we do not know -> none
    wanted = parse_langs(settings.subtitle_langs)
    return wanted if (wanted or not settings.subtitle_langs.strip()) else ["-"]


def build_plan(raw_subtitles, request_enabled, request_langs) -> Plan | None:
    """None = do not fetch any subtitles for this item (disabled, nothing to fetch, or nothing allowed)."""
    enabled = settings.subtitles_enabled if request_enabled is None else request_enabled
    if not enabled:
        return None
    allow = _allow_list(request_langs, settings.subtitle_langs)
    plan = plan_subtitles(raw_subtitles, allow_langs=allow, max_files=settings.subtitle_max_files)
    return plan if plan.candidates else None


def default_lang(request_default) -> str | None:
    value = request_default or settings.subtitle_default_lang
    code = normalize_lang(value) if value else "und"
    return None if code == "und" else code


async def _save(item_id: str, tracks: list[dict], status: str) -> None:
    async with SessionLocal() as db:
        await db.execute(update(LibraryItem).where(LibraryItem.id == item_id)
                         .values(subtitles=tracks, subtitles_status=status))
        await db.commit()


async def _finish_pack(item_id: str) -> None:
    """A task-wide subtitle pack has no video: when its subtitles are settled it is simply 'ready', and its
    time-to-live starts (the normal library cleanup then deletes it, files included)."""
    async with SessionLocal() as db:
        it = await db.get(LibraryItem, item_id)
        if it is not None and it.status == "downloading":
            now = utcnow()
            it.status = "ready"
            it.ready_at = now
            it.delete_at = now + timedelta(hours=it.ttl_hours)
            it.size = 0
            await db.commit()


async def _job(item_id: str, plan: Plan, headers: dict | None, default: str | None, pack: bool = False) -> None:
    try:
        params = Params(max_files=settings.subtitle_max_files, max_bytes=settings.subtitle_max_bytes,
                        timeout_s=settings.subtitle_timeout_s, ffmpeg_fallback=settings.subtitle_ffmpeg_fallback,
                        ffmpeg=settings.ffmpeg_path, default_lang=default, sem=_semaphore())
        await run_subtitles(plan, item_dir(item_id), params, headers, lambda tracks, status: _save(item_id, tracks, status))
    except asyncio.CancelledError:
        raise                                   # the item was deleted / failed: nothing to save
    except Exception:
        traceback.print_exc()
        try:
            async with SessionLocal() as db:
                it = await db.get(LibraryItem, item_id)
                if it is not None:
                    it.subtitles_status = "partial" if it.subtitles else "none"
                    await db.commit()
        except Exception:
            traceback.print_exc()
    if pack:                                    # (not reached on cancellation: the pack was deleted)
        try:
            await _finish_pack(item_id)
        except Exception:
            traceback.print_exc()


def start_subtitles(item_id: str, plan: Plan, headers: dict | None, default: str | None, pack: bool = False) -> None:
    t = asyncio.create_task(_job(item_id, plan, headers, default, pack))
    jobs[item_id] = t
    t.add_done_callback(lambda _: jobs.pop(item_id, None))


def cancel_subtitles(item_id: str) -> None:
    t = jobs.get(item_id)
    if t and not t.done():
        t.cancel()


async def settle_pending() -> None:
    """After a restart no subtitle job is running: 'pending' can never finish, so make it honest."""
    async with SessionLocal() as db:
        res = await db.execute(select(LibraryItem).where(LibraryItem.subtitles_status == "pending"))
        for it in res.scalars():
            it.subtitles_status = "partial" if it.subtitles else "none"
        await db.commit()
