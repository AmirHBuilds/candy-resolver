import asyncio
import time
import traceback

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import selectinload

from . import signals
from .config import settings
from .db import SessionLocal
from .models import Script, Source, SourceRun, Stream, Task, as_utc, public_label, utcnow
from .runner import ScriptError, run_script
from .schemas import SourceResultOut, StreamOut, TaskOut

# Keep references so background tasks are not garbage-collected mid-run.
running: dict[str, asyncio.Task] = {}


def start_task(task_id: str) -> asyncio.Task:
    signals.register(task_id)
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
    pending: list[asyncio.Future] = []
    try:
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return
            rows = (await db.execute(
                select(Source, Script)
                .join(Script, Script.source_id == Source.id)
                .where(Source.enabled.is_(True), Script.active.is_(True))
                .order_by(Source.id)
            )).all()

            task.status = "running"
            task.sources_total = len(rows)
            task.starred_total = sum(1 for s, _ in rows if s.starred)
            if not rows:
                task.status = "done"
                task.version = (task.version or 0) + 1
                await db.commit()
                return
            await db.commit()

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

            pending = [asyncio.ensure_future(one(s, sc)) for s, sc in rows]
            any_ok, finished = False, 0
            # Save each source's result the moment it finishes, so clients can see it right away.
            for fut in asyncio.as_completed(pending):
                source, script, error, result, ms = await fut
                streams = clean_streams(result.get("streams"))
                status = "error" if error else ("ok" if streams else "empty")
                any_ok = any_ok or status != "error"
                finished += 1
                db.add(SourceRun(
                    task_id=task.id, source_id=source.id, source_name=source.name,
                    public_name=public_label(source.public_name, source.id), starred=bool(source.starred),
                    script_version=script.version, status=status, error=error, duration_ms=ms,
                    subtitles=clean_subtitles(result.get("subtitles")),
                    audio=clean_audio(result.get("audio")),
                    streams=[Stream(**s) for s in streams],
                ))
                task.version = (task.version or 0) + 1
                if finished == len(rows):
                    task.status = "done" if any_ok else "failed"
                await db.commit()
                signals.notify(task_id)
    except Exception:
        traceback.print_exc()
        try:
            async with SessionLocal() as s2:
                t = await s2.get(Task, task_id)
                if t is not None and t.status not in ("done", "failed"):
                    t.status = "failed"
                    t.version = (t.version or 0) + 1
                    await s2.commit()
        except Exception:
            traceback.print_exc()
        signals.notify(task_id)
    finally:
        for p in pending:
            if not p.done():
                p.cancel()
        signals.unregister(task_id)


async def peek(task_id: str) -> dict | None:
    """Small state snapshot for waiters. Uses its own short session so no DB connection is held while waiting."""
    async with SessionLocal() as s:
        row = (await s.execute(
            select(Task.status, Task.version, Task.api_key_id, Task.expires_at, Task.starred_total)
            .where(Task.id == task_id))).first()
        if row is None:
            return None
        ok = SourceRun.status == "ok"
        counts = (await s.execute(
            select(
                func.coalesce(func.sum(case((ok, 1), else_=0)), 0),
                func.coalesce(func.sum(case((ok & SourceRun.starred.is_(True), 1), else_=0)), 0),
                func.coalesce(func.sum(case((SourceRun.starred.is_(True), 1), else_=0)), 0),
            ).where(SourceRun.task_id == task_id))).one()
    return {"status": row.status, "version": row.version or 0, "api_key_id": row.api_key_id,
            "has_streams": counts[0] > 0, "has_starred_streams": counts[1] > 0,
            "starred_done": int(counts[2]), "starred_total": row.starred_total or 0,
            "expired": as_utc(row.expires_at) < utcnow()}


async def fetch_task(db, task_id: str) -> Task | None:
    res = await db.execute(
        select(Task)
        .where(Task.id == task_id)
        .options(selectinload(Task.runs).selectinload(SourceRun.streams))
        .execution_options(populate_existing=True)
    )
    return res.scalar_one_or_none()


def public_error(err: str | None) -> str | None:
    """Clients never see raw script errors (they can contain URLs, paths, site names)."""
    if not err:
        return None
    return "timeout" if err.startswith("timed out") else "failed"


def task_to_out(task: Task) -> TaskOut:
    return TaskOut(
        task_id=task.id, status=task.status, tmdb_id=task.tmdb_id, type=task.media_type,
        season=task.season, episode=task.episode, title=(task.meta or {}).get("title"),
        created_at=task.created_at, expires_at=task.expires_at,
        version=task.version or 0, sources_total=task.sources_total or 0, sources_done=len(task.runs),
        sources=[
            SourceResultOut(
                source=r.public_name or public_label(None, r.source_id), starred=bool(r.starred), status=r.status,
                error=public_error(r.error), duration_ms=r.duration_ms,
                streams=[StreamOut(id=s.id, quality=s.quality, format=s.format, url=s.url,
                                   size=s.size, headers=s.headers) for s in r.streams],
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
