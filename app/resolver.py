import logging
import re
import asyncio
import time
import traceback
from datetime import timedelta

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import selectinload

from . import signals
from .scheduler import queue, source_slot
from .config import settings
from .db import SessionLocal
from .models import Script, Source, SourceRun, Stream, Task, as_utc, public_label, utcnow
from .ranked import run_ranked
from .ranking import order_rows
from .linkexpiry import link_expiry
from .notify import notifier
from .streaks import StreakTracker
from .runner import ScriptError, run_script
from .schemas import SourceResultOut, StreamOut, TaskOut, TaskSummary
from .taskstate import effective_status, results_view, stream_alive

log = logging.getLogger("candy.resolver")

# Keep references so background tasks are not garbage-collected mid-run.
running: dict[str, asyncio.Task] = {}


def start_task(task_id: str, only: set[int] | None = None) -> asyncio.Task:
    """only = source ids: run just these (a refresh); None = every source not asked yet."""
    signals.register(task_id)
    t = asyncio.create_task(_scheduled(task_id, only))
    running[task_id] = t
    t.add_done_callback(lambda _: running.pop(task_id, None))
    return t


async def _scheduled(task_id: str, only: set[int] | None = None) -> None:
    """Wait in line for a run slot (MAX_CONCURRENT_TASKS), showing status "queued" meanwhile, then run."""
    async def mark_queued():
        try:
            async with SessionLocal() as db:
                t = await db.get(Task, task_id)
                if t is not None and t.status == "pending":
                    t.status = "queued"
                    t.version = (t.version or 0) + 1
                    await db.commit()
            signals.notify(task_id)
        except Exception:                      # best effort: the task still runs when its turn comes
            traceback.print_exc()

    try:
        async with queue.slot(task_id, mark_queued):
            await _run(task_id, only)
    except asyncio.CancelledError:
        signals.unregister(task_id)
        raise


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
            "expires_at": link_expiry(s["url"], s.get("headers") if isinstance(s.get("headers"), dict) else None),
            "extra": s["extra"] if isinstance(s.get("extra"), dict) else None,
        })
    return out


def clean_subtitles(raw) -> list[dict]:
    return [x for x in (raw or [])[:100] if isinstance(x, dict) and _http(x.get("url"))]


def clean_audio(raw) -> list[dict]:
    return [x for x in (raw or [])[:50] if isinstance(x, dict)]


_streaks = StreakTracker()          # consecutive failed runs per source (in memory)


def _what(task) -> str:
    """\"Obsession (movie, tmdb 1339713)\" / \"Show (tv S01E02, tmdb 1)\"."""
    title = (task.meta or {}).get("title") or "?"
    se = f" S{task.season:02d}E{task.episode:02d}" if task.season is not None and task.episode is not None else ""
    return f"{title} ({task.media_type}{se}, tmdb {task.tmdb_id})"


def _error_sig(error: str) -> str:
    """Same failure -> same key, so one broken source does not send a message per task (digits and urls vary)."""
    last = (error.strip().splitlines() or [""])[-1]
    return re.sub(r"https?://\S+|\d+", "#", last)[:100]


def _outcome(error, result) -> tuple[str, list[dict]]:
    streams = clean_streams((result or {}).get("streams"))
    return ("error" if error else ("ok" if streams else "empty")), streams


async def _run(task_id: str, only: set[int] | None = None) -> None:
    """Run the task's sources in the admin's rank order. With sources_wanted = N only as many sources run as are still
    needed to reach N sources with streams (N=1: the best source first, the next one only if it found nothing).
    Called again by POST /v1/tasks/{id}/sources: sources that already ran for this task are skipped.
    With `only` (a refresh) just those sources run, all of them, whatever sources_wanted says."""
    try:
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return
            prior = (await db.execute(
                select(SourceRun.source_id, SourceRun.status, SourceRun.starred).where(SourceRun.task_id == task_id))).all()
            tried = {p[0] for p in prior}
            rows = (await db.execute(
                select(Source, Script)
                .join(Script, Script.source_id == Source.id)
                .where(Source.enabled.is_(True), Script.active.is_(True))
            )).all()
            rows = [r for r in order_rows(rows, bool(task.starred_first)) if r[0].id not in tried and (only is None or r[0].id in only)]

            task.status = "running"
            task.sources_total = len(prior) + len(rows)
            task.starred_total = sum(1 for p in prior if p[2]) + sum(1 for s, _ in rows if s.starred)
            if not rows:
                task.status = "done"
                task.version = (task.version or 0) + 1
                await db.commit()
                return
            await db.commit()

            ctx = task.meta
            sem = asyncio.Semaphore(settings.max_parallel_sources)
            state = {"any_ok": any(p[1] != "error" for p in prior)}

            async def one(pair):
                source, script = pair
                # Browser sources first wait for a browser slot (server-wide), THEN for a normal source slot, so a
                # queued browser source never blocks the light sources of its own task. The clock starts after both.
                async with source_slot(source.uses_browser):
                    async with sem:
                        t0 = time.monotonic()
                        stats: dict = {}
                        try:
                            result = await run_script(script.code, ctx, source.timeout_s, stats)
                            if not isinstance(result, dict):
                                raise ScriptError("script must return an object")
                            return source, script, None, result, int((time.monotonic() - t0) * 1000), stats.get("peak_mb")
                        except ScriptError as e:
                            return source, script, str(e)[:2000], {}, int((time.monotonic() - t0) * 1000), stats.get("peak_mb")
                        except Exception as e:  # never let one source break the task
                            return source, script, f"internal error: {e}"[:2000], {}, int((time.monotonic() - t0) * 1000), stats.get("peak_mb")

            # Save each source's result the moment it finishes, so clients can see it right away.
            async def on_result(res):
                source, script, error, result, ms, peak = res
                status, streams = _outcome(error, result)
                state["any_ok"] = state["any_ok"] or status != "error"
                verdict, n_fail = _streaks.record(source.id, status == "error", int(notifier.cfg.get("fail_streak", 5) or 0))
                if verdict == "failing":
                    notifier.emit("source_errors", f"Source keeps failing: {source.name}",
                                  {"Failed runs in a row": n_fail, "Last task": _what(task), "Script": f"v{script.version}",
                                   "Tip": "open it in admin -> Sources and press Test run"},
                                  (error or "").strip()[-1000:], key=f"streak:{source.id}", dedupe_s=6 * 3600)
                elif verdict == "recovered":
                    notifier.emit("source_errors", f"Source works again: {source.name}", {"After failed runs": n_fail, "Last task": _what(task)},
                                  level="info", key=f"recovered:{source.id}", dedupe_s=0)
                if error:
                    log.warning("task %s: source '%s' (script v%s) failed after %d ms: %s", task.id, source.name,
                                script.version, ms, error.strip().splitlines()[-1][:300] if error.strip() else error)
                    notifier.emit("source_errors", f"Source failed: {source.name}",
                                  {"What": _what(task), "Task": task.id, "Script": f"v{script.version}", "Time": f"{ms} ms"},
                                  error.strip()[-1200:], key=f"src:{source.id}:{_error_sig(error)}")
                else:
                    log.info("task %s: source '%s' (script v%s) -> %s, %d streams, %d ms%s", task.id, source.name,
                             script.version, status, len(streams), ms, f", peak {peak} MB" if peak else "")
                db.add(SourceRun(
                    task_id=task.id, source_id=source.id, source_name=source.name,
                    public_name=public_label(source.public_name, source.id), starred=bool(source.starred),
                    script_version=script.version, status=status, error=error, duration_ms=ms, peak_mb=peak,
                    subtitles=clean_subtitles(result.get("subtitles")),
                    audio=clean_audio(result.get("audio")),
                    streams=[Stream(**s) for s in streams],
                ))
                task.version = (task.version or 0) + 1
                await db.commit()
                signals.notify(task_id)

            await run_ranked(rows, one, lambda res: _outcome(res[2], res[3])[0] == "ok", on_result,
                             wanted=None if only is not None else task.sources_wanted, ok_so_far=sum(1 for p in prior if p[1] == "ok"))
            task.status = "done" if state["any_ok"] else "failed"
            task.version = (task.version or 0) + 1
            await db.commit()
            signals.notify(task_id)
            if task.status == "failed":
                notifier.emit("source_errors", "Task failed: no source worked", {"What": _what(task), "Task": task.id, "Sources tried": task.sources_total},
                              key=f"taskfail:{task.tmdb_id}:{task.media_type}:{task.season}:{task.episode}")
    except Exception:
        traceback.print_exc()
        notifier.emit("server_errors", "Task crashed", {"Task": task_id}, traceback.format_exc()[-1500:], key=f"crash:{task_id}")
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
            "has_streams": counts[0] > 0, "has_starred_streams": counts[1] > 0, "ok_runs": int(counts[0]),
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
    """Once the results have expired the task is still returned (movie info, status "expired", its library items),
    but never with stale source links: sources is empty even if the cleanup job has not run yet.
    Links are also judged one by one: a link past its own expiry, or one a download found dead, is not offered. The task
    only turns "expired" when none of its links is left; `expires_at` is when the last live link stops working."""
    now = utcnow()
    status = effective_status(task.status, task.expires_at)
    expires_at = task.expires_at
    if status == "done":
        pairs = [(as_utc(s.expires_at) if s.expires_at else None, bool(s.dead)) for r in task.runs for s in r.streams]
        alive, last = results_view(pairs, task.expires_at, now)
        if pairs and alive == 0:
            status = "expired"
        elif last is not None:
            expires_at = last
    expired = status == "expired"

    def live(r):
        return [s for s in r.streams if stream_alive(as_utc(s.expires_at) if s.expires_at else None, bool(s.dead), now)]

    return TaskOut(
        task_id=task.id, status=status, tmdb_id=task.tmdb_id, type=task.media_type,
        season=task.season, episode=task.episode, title=(task.meta or {}).get("title"),
        created_at=task.created_at, expires_at=expires_at,
        version=task.version or 0, sources_total=task.sources_total or 0, sources_done=len(task.runs),
        sources_wanted=task.sources_wanted, sources_found=sum(1 for r in task.runs if r.status == "ok"),
        has_more=status in ("done", "failed") and (task.sources_total or 0) > len(task.runs),
        queue_position=queue.position(task.id),
        sources=[] if expired else [
            SourceResultOut(
                source=r.public_name or public_label(None, r.source_id), starred=bool(r.starred),
                status="expired" if (r.status == "ok" and r.streams and not live(r)) else r.status,
                error=public_error(r.error), duration_ms=r.duration_ms,
                streams=[StreamOut(id=s.id, quality=s.quality, format=s.format, url=s.url, size=s.size, headers=s.headers,
                                   expires_at=as_utc(s.expires_at) if s.expires_at else None) for s in live(r)],
                subtitles=r.subtitles or [], audio=r.audio or [],
            )
            for r in task.runs
        ],
    )


def task_summary(task: Task, sources_done: int) -> TaskSummary:
    return TaskSummary(
        task_id=task.id, status=effective_status(task.status, task.expires_at), tmdb_id=task.tmdb_id,
        type=task.media_type, season=task.season, episode=task.episode, title=(task.meta or {}).get("title"),
        created_at=task.created_at, expires_at=task.expires_at, version=task.version or 0,
        sources_total=task.sources_total or 0, sources_done=sources_done, queue_position=queue.position(task.id))


async def fail_stuck_tasks() -> None:
    """On startup: tasks that were mid-run when the server stopped can never finish."""
    async with SessionLocal() as db:
        await db.execute(update(Task).where(Task.status.in_(["pending", "queued", "running"])).values(status="failed"))
        await db.commit()


async def expire_task_results() -> int:
    """After task_ttl_hours the results are useless (links go stale), so delete them but KEEP the task:
    its movie info, status "expired" and its library items stay visible. The per-source run summaries
    (status, timing) stay for the admin health view; only streams and original subtitle/audio links go."""
    async with SessionLocal() as db:
        res = await db.execute(
            select(Task).where(Task.expires_at < utcnow(), Task.status != "expired").limit(500)
            .options(selectinload(Task.runs).selectinload(SourceRun.streams)))
        tasks = res.scalars().all()
        for t in tasks:
            for r in t.runs:
                r.streams.clear()          # delete-orphan cascade removes the rows
                r.subtitles = []
                r.audio = []
            t.status = "expired"
            t.version = (t.version or 0) + 1
        await db.commit()
        return len(tasks)


async def purge_old_tasks() -> int:
    """The task record itself goes only after task_retention_days (0 = never). Library items are independent."""
    if settings.task_retention_days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=settings.task_retention_days)
    async with SessionLocal() as db:
        res = await db.execute(
            select(Task).where(Task.created_at < cutoff).limit(500)
            .options(selectinload(Task.runs).selectinload(SourceRun.streams)))
        tasks = res.scalars().all()
        for t in tasks:
            await db.delete(t)
        await db.commit()
        return len(tasks)


def is_expired(task: Task) -> bool:
    return as_utc(task.expires_at) < utcnow()
