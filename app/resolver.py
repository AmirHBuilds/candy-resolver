import asyncio
import time

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from .config import settings
from .db import SessionLocal
from .models import Script, Source, SourceRun, Stream, Task, as_utc, utcnow
from .runner import ScriptError, run_script
from .schemas import SourceResultOut, StreamOut, TaskOut

# Keep references so background tasks are not garbage-collected mid-run.
running: dict[str, asyncio.Task] = {}


def start_task(task_id: str) -> asyncio.Task:
    t = asyncio.create_task(_run(task_id))
    running[task_id] = t
    t.add_done_callback(lambda _: running.pop(task_id, None))
    return t


def _s(v):
    return v if isinstance(v, str) and v else None


def _http(url) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def clean_streams(raw) -> list[dict]:
    out = []
    for s in (raw or [])[:100]:
        if not isinstance(s, dict) or not _http(s.get("url")):
            continue
        size = s.get("size")
        out.append({
            "url": s["url"],
            "quality": _s(s.get("quality")),
            "format": _s(s.get("format")),
            "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
            "headers": s["headers"] if isinstance(s.get("headers"), dict) else None,
            "extra": s["extra"] if isinstance(s.get("extra"), dict) else None,
        })
    return out


def clean_subtitles(raw) -> list[dict]:
    return [x for x in (raw or [])[:100] if isinstance(x, dict) and _http(x.get("url"))]


def clean_audio(raw) -> list[dict]:
    return [x for x in (raw or [])[:50] if isinstance(x, dict)]


async def _run(task_id: str) -> None:
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        if task is None:
            return
        try:
            task.status = "running"
            await db.commit()

            rows = (await db.execute(
                select(Source, Script)
                .join(Script, Script.source_id == Source.id)
                .where(Source.enabled.is_(True), Script.active.is_(True))
                .order_by(Source.id)
            )).all()

            ctx = task.meta
            sem = asyncio.Semaphore(settings.max_parallel_sources)

            async def one(source: Source, script: Script):
                async with sem:
                    t0 = time.monotonic()
                    try:
                        result = await run_script(script.code, ctx, source.timeout_s)
                        if not isinstance(result, dict):
                            raise ScriptError("script must return an object")
                        return source, script, None, result, int((time.monotonic() - t0) * 1000)
                    except ScriptError as e:
                        return source, script, str(e)[:2000], {}, int((time.monotonic() - t0) * 1000)
                    except Exception as e:  # never let one source break the task
                        return source, script, f"internal error: {e}"[:2000], {}, int((time.monotonic() - t0) * 1000)

            results = await asyncio.gather(*(one(s, sc) for s, sc in rows))

            any_ok = not results
            for source, script, error, result, ms in results:
                streams = clean_streams(result.get("streams"))
                status = "error" if error else ("ok" if streams else "empty")
                any_ok = any_ok or status != "error"
                run = SourceRun(
                    task_id=task.id, source_id=source.id, source_name=source.name,
                    script_version=script.version, status=status, error=error, duration_ms=ms,
                    subtitles=clean_subtitles(result.get("subtitles")),
                    audio=clean_audio(result.get("audio")),
                    streams=[Stream(**s) for s in streams],
                )
                db.add(run)

            task.status = "done" if any_ok else "failed"
            await db.commit()
        except Exception:
            await db.rollback()
            task = await db.get(Task, task_id)
            if task is not None:
                task.status = "failed"
                await db.commit()
            raise


async def fetch_task(db, task_id: str) -> Task | None:
    res = await db.execute(
        select(Task)
        .where(Task.id == task_id)
        .options(selectinload(Task.runs).selectinload(SourceRun.streams))
        .execution_options(populate_existing=True)
    )
    return res.scalar_one_or_none()


def task_to_out(task: Task) -> TaskOut:
    return TaskOut(
        task_id=task.id, status=task.status, tmdb_id=task.tmdb_id, type=task.media_type,
        season=task.season, episode=task.episode, title=(task.meta or {}).get("title"),
        created_at=task.created_at, expires_at=task.expires_at,
        sources=[
            SourceResultOut(
                source=r.source_name, status=r.status, error=r.error, duration_ms=r.duration_ms,
                streams=[StreamOut(id=s.id, quality=s.quality, format=s.format, url=s.url,
                                   size=s.size, headers=s.headers, extra=s.extra) for s in r.streams],
                subtitles=r.subtitles or [], audio=r.audio or [],
            )
            for r in task.runs
        ],
    )


async def fail_stuck_tasks() -> None:
    """On startup: tasks that were mid-run when the server stopped can never finish."""
    async with SessionLocal() as db:
        await db.execute(update(Task).where(Task.status.in_(["pending", "running"])).values(status="failed"))
        await db.commit()


async def purge_expired() -> int:
    async with SessionLocal() as db:
        res = await db.execute(
            select(Task)
            .where(Task.expires_at < utcnow())
            .options(selectinload(Task.runs).selectinload(SourceRun.streams))
        )
        tasks = res.scalars().all()
        for t in tasks:
            await db.delete(t)
        await db.commit()
        return len(tasks)


def is_expired(task: Task) -> bool:
    return as_utc(task.expires_at) < utcnow()
