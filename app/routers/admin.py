import ast
import asyncio
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..auth import generate_key, hash_key, require_admin
from ..config import settings
from ..db import get_db
from .. import speedmeter, testlog
from ..downloader import cancel_download
from ..itemfiles import remove_item_dir
from ..subs_job import cancel_subtitles
from ..browserflag import active_code, apply_detection, detect_browser
from ..models import ApiKey, LibraryItem, Script, Source, SourceRun, Task
from ..ranking import order_key, source_stats
from ..runner import ScriptError, run_script
from ..scheduler import queue, source_slot
from ..sourcexfer import build_export, code_hash, parse_import, plan
from ..schemas import (ApiKeyIn, ApiKeyUpdate, ScriptIn, SourceIn, SourceUpdate, TestIn)
from ..tmdb import TmdbError, TmdbNotFound, build_meta

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

MAX_CODE_CHARS = 200_000


def validate_code(code: str) -> None:
    if len(code) > MAX_CODE_CHARS:
        raise HTTPException(422, "script too large")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise HTTPException(422, f"syntax error on line {e.lineno}: {e.msg}")
    if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "resolve"
               for n in tree.body):
        raise HTTPException(422, "script must define a top-level resolve(ctx) function")


def source_dict(s: Source, peaks: dict | None = None, times: dict | None = None) -> dict:
    active = next((sc.version for sc in s.scripts if sc.active), None)
    pk = (peaks or {}).get(s.id) or {}
    tm = (times or {}).get(s.id) or source_stats([])
    return {"id": s.id, "name": s.name, "public_name": s.public_name, "starred": s.starred, "rank": s.rank, "base_url": s.base_url, "language": s.language,
            **tm,
            "enabled": s.enabled, "timeout_s": s.timeout_s, "active_version": active,
            "uses_browser": bool(s.uses_browser), "browser_manual": bool(s.browser_manual),
            "browser_detected": detect_browser(active_code(s)),
            "peak_mb_max": pk.get("max"), "peak_mb_avg": pk.get("avg"), "peak_runs": pk.get("n", 0),
            "created_at": s.created_at}


async def peak_stats(db: AsyncSession) -> dict:
    """Peak RAM per source over the runs still in the history: {source_id: {max, avg, n}}."""
    rows = (await db.execute(
        select(SourceRun.source_id, func.max(SourceRun.peak_mb), func.avg(SourceRun.peak_mb), func.count(SourceRun.peak_mb))
        .where(SourceRun.peak_mb.is_not(None), SourceRun.source_id.is_not(None)).group_by(SourceRun.source_id))).all()
    return {sid: {"max": mx, "avg": int(av) if av is not None else None, "n": n} for sid, mx, av, n in rows}


async def time_stats(db: AsyncSession) -> dict:
    """Response time per source from the runs still in the history: {source_id: {last_ms, avg_ms, found_rate, ...}}."""
    rows = (await db.execute(
        select(SourceRun.source_id, SourceRun.status, SourceRun.duration_ms)
        .where(SourceRun.source_id.is_not(None)).order_by(SourceRun.id.desc()).limit(20000))).all()
    per: dict[int, list] = {}
    for sid, status, ms in rows:                       # newest first
        per.setdefault(sid, []).append((status, ms))
    return {sid: source_stats(runs) for sid, runs in per.items()}


async def get_source(db: AsyncSession, source_id: int) -> Source:
    res = await db.execute(select(Source).where(Source.id == source_id)
                           .options(selectinload(Source.scripts)))
    s = res.scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "source not found")
    return s


# ---------------- sources ----------------
@router.get("/sources")
async def list_sources(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Source).options(selectinload(Source.scripts)).order_by(Source.id))
    peaks, times = await peak_stats(db), await time_stats(db)
    return [source_dict(s, peaks, times) for s in sorted(res.scalars(), key=order_key)]   # the order they are tried in


# ---------------- export / import ----------------
def _export_row(s: Source) -> dict:
    return {"name": s.name, "public_name": s.public_name, "base_url": s.base_url, "language": s.language, "enabled": s.enabled,
            "starred": s.starred, "rank": s.rank, "timeout_s": s.timeout_s, "uses_browser": bool(s.uses_browser),
            "browser_manual": bool(s.browser_manual),
            "scripts": [{"version": x.version, "note": x.note, "active": x.active, "code": x.code,
                         "created_at": x.created_at.isoformat() if x.created_at else None} for x in s.scripts]}


@router.get("/sources/export")
async def export_sources(request: Request, include: Literal["active", "all"] = Query("active", description="active = only each source's active script; all = every version"),
                         ids: str | None = Query(None, description="Comma separated source ids. Empty = all sources"),
                         db: AsyncSession = Depends(get_db)):
    """Download sources (settings + scripts) as one JSON file, to back them up or move them to another server."""
    want = None
    if ids:
        try:
            want = {int(x) for x in ids.split(",") if x.strip()}
        except ValueError:
            raise HTTPException(422, "ids must be numbers separated by commas")
    res = await db.execute(select(Source).options(selectinload(Source.scripts)).order_by(Source.id))
    rows = [_export_row(s) for s in res.scalars() if want is None or s.id in want]
    if not rows:
        raise HTTPException(404, "no sources to export")
    now = datetime.now(timezone.utc)
    payload = build_export(rows, include, request.app.version, now.isoformat(timespec="seconds"))
    return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="candyresolver-sources-{now:%Y%m%d-%H%M}.json"', "Cache-Control": "no-store"})


class ImportIn(BaseModel):
    data: dict                                               # the exported file
    mode: Literal["skip", "update", "copy"] = "skip"         # what to do with a source whose name already exists
    dry_run: bool = False                                    # true = only report what would happen


def _validate_for_import(code: str) -> None:
    try:
        validate_code(code)
    except HTTPException as e:
        raise ValueError(e.detail)


@router.post("/sources/import")
async def import_sources(body: ImportIn, db: AsyncSession = Depends(get_db)):
    """Import sources from an exported file. Scripts are checked like an upload (syntax, resolve(ctx)); one bad source never stops
    the others. mode: skip (leave existing ones alone), update (settings + new script versions), copy (add as a new source)."""
    try:
        entries, problems = parse_import(body.data, _validate_for_import)
    except ValueError as e:
        raise HTTPException(422, str(e))
    res = await db.execute(select(Source).options(selectinload(Source.scripts)))
    by_name = {s.name: s for s in res.scalars()}
    actions = plan(entries, {n: {code_hash(x.code) for x in s.scripts} for n, s in by_name.items()}, body.mode)
    items = []
    for a in actions:
        e = a["entry"]
        item = {"name": e["name"], "final_name": a["final_name"], "action": a["action"], "detail": a["detail"],
                "scripts_added": len(a["new_scripts"]), "uses_browser": e["uses_browser"]}
        if not body.dry_run and a["action"] != "skip":
            active_hash = next((code_hash(x["code"]) for x in e["scripts"] if x["active"]), None)
            if a["action"] == "update":
                s = by_name[e["name"]]
                for k in ("public_name", "base_url", "language", "enabled", "starred", "rank", "timeout_s"):
                    setattr(s, k, e[k])
                version = max((x.version for x in s.scripts), default=0)
                for x in a["new_scripts"]:
                    version += 1
                    s.scripts.append(Script(version=version, code=x["code"], note=x["note"], active=False))
            else:
                s = Source(name=a["final_name"], **{k: e[k] for k in ("public_name", "base_url", "language", "enabled", "starred", "rank", "timeout_s")})
                s.scripts = [Script(version=i + 1, code=x["code"], note=x["note"], active=False) for i, x in enumerate(a["new_scripts"])]
                db.add(s)
            if active_hash:
                for x in s.scripts:
                    x.active = code_hash(x.code) == active_hash
            if e["browser_manual"]:
                s.uses_browser, s.browser_manual = e["uses_browser"], True
            else:
                apply_detection(s, active_code(s))             # follows the script unless you set it by hand on this server
        items.append(item)
    for pr in problems:
        items.append({"name": pr["name"], "final_name": pr["name"], "action": "error", "detail": pr["error"], "scripts_added": 0, "uses_browser": False})
    if not body.dry_run:
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "a source name clashes with an existing one: nothing was imported (try mode 'copy')")
    count = lambda act: sum(1 for i in items if i["action"] == act)
    return {"dry_run": body.dry_run, "mode": body.mode, "created": count("create"), "updated": count("update"), "copied": count("copy"),
            "skipped": count("skip"), "errors": count("error"), "items": items}


@router.post("/sources", status_code=201)
async def create_source(body: SourceIn, db: AsyncSession = Depends(get_db)):
    data = body.model_dump()
    flag = data.pop("uses_browser")
    s = Source(**data)
    if flag is not None:                 # chosen by hand at creation: auto-detection will leave it alone
        s.uses_browser, s.browser_manual = flag, True
    s.scripts = []
    db.add(s)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "a source with this name already exists")
    return source_dict(s)


@router.patch("/sources/{source_id}")
async def update_source(source_id: int, body: SourceUpdate, db: AsyncSession = Depends(get_db)):
    s = await get_source(db, source_id)
    data = body.model_dump(exclude_unset=True)
    if "rank" in data:                     # null is a real value here: it clears the rank
        s.rank = data.pop("rank")
    auto = data.pop("browser_auto", None)
    if data.get("uses_browser") is not None:
        s.browser_manual = True            # the admin decided: uploads no longer change it
    for k, v in data.items():
        if v is not None:
            setattr(s, k, v)
    if auto:                               # "use auto-detect again"
        s.browser_manual = False
        s.uses_browser = detect_browser(active_code(s))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "a source with this name already exists")
    return source_dict(s, await peak_stats(db), await time_stats(db))


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(source_id: int, db: AsyncSession = Depends(get_db)):
    s = await get_source(db, source_id)
    await db.delete(s)
    await db.commit()


# ---------------- scripts ----------------
@router.get("/sources/{source_id}/scripts")
async def list_scripts(source_id: int, db: AsyncSession = Depends(get_db)):
    s = await get_source(db, source_id)
    return [{"version": sc.version, "active": sc.active, "note": sc.note,
             "chars": len(sc.code), "created_at": sc.created_at} for sc in s.scripts]


@router.get("/sources/{source_id}/scripts/{version}")
async def get_script(source_id: int, version: int, db: AsyncSession = Depends(get_db)):
    s = await get_source(db, source_id)
    sc = next((x for x in s.scripts if x.version == version), None)
    if sc is None:
        raise HTTPException(404, "script version not found")
    return {"version": sc.version, "active": sc.active, "note": sc.note, "code": sc.code,
            "created_at": sc.created_at}


@router.post("/sources/{source_id}/scripts", status_code=201)
async def upload_script(source_id: int, body: ScriptIn, db: AsyncSession = Depends(get_db)):
    validate_code(body.code)
    s = await get_source(db, source_id)
    version = max((x.version for x in s.scripts), default=0) + 1
    if body.activate:
        for x in s.scripts:
            x.active = False
    db.add(Script(source_id=s.id, version=version, code=body.code, note=body.note,
                  active=body.activate))
    if body.activate:
        apply_detection(s, body.code)      # follows the new script unless the admin set it by hand
    await db.commit()
    return {"version": version, "active": body.activate, "uses_browser": bool(s.uses_browser)}


@router.post("/sources/{source_id}/scripts/{version}/activate")
async def activate_script(source_id: int, version: int, db: AsyncSession = Depends(get_db)):
    s = await get_source(db, source_id)
    target = next((x for x in s.scripts if x.version == version), None)
    if target is None:
        raise HTTPException(404, "script version not found")
    for x in s.scripts:
        x.active = x is target
    apply_detection(s, target.code)
    await db.commit()
    return {"active_version": version, "uses_browser": bool(s.uses_browser)}


@router.post("/sources/{source_id}/test")
async def test_source(source_id: int, body: TestIn, db: AsyncSession = Depends(get_db)):
    """Run a script (active version by default) right now and show the raw result. For development."""
    s = await get_source(db, source_id)
    sc = (next((x for x in s.scripts if x.version == body.version), None) if body.version
          else next((x for x in s.scripts if x.active), None))
    if sc is None:
        raise HTTPException(404, "no such script version (or no active script)")
    try:
        meta = await build_meta(body.type, body.tmdb_id, body.season, body.episode)
    except TmdbNotFound:
        raise HTTPException(404, "TMDB id not found")
    except TmdbError as e:
        raise HTTPException(502, str(e))
    stats: dict = {}
    async with source_slot(s.uses_browser):          # a test is as heavy as a real run: it takes its turn too
        t0 = time.monotonic()
        try:
            result = await run_script(sc.code, meta, s.timeout_s, stats)
            return {"ok": True, "version": sc.version, "duration_ms": int((time.monotonic() - t0) * 1000),
                    "peak_mb": stats.get("peak_mb"), "context": meta, "result": result}
        except ScriptError as e:
            return {"ok": False, "version": sc.version, "duration_ms": int((time.monotonic() - t0) * 1000),
                    "peak_mb": stats.get("peak_mb"), "context": meta, "error": str(e)}


# ---------------- live test (same run as /test, but the log can be followed while it runs) ----------------
async def _run_test(run: "testlog.TestRun", code: str, version: int, timeout_s: int, uses_browser: bool, body: TestIn) -> None:
    stats: dict = {}
    ticker = None
    t0 = time.monotonic()

    async def watch_memory():                              # one line whenever memory grows noticeably
        last = 0
        while True:
            await asyncio.sleep(2)
            mb = stats.get("peak_mb") or 0
            if mb >= last + 50 or (last == 0 and mb):
                last = mb
                run.log("runner", f"memory: peak {mb} MB, up to {stats.get('peak_procs', 1)} process(es)")

    def done(result: dict, status: str = "done") -> None:
        result.update(version=version, duration_ms=int((time.monotonic() - t0) * 1000), peak_mb=stats.get("peak_mb"))
        run.finish(status, result)

    try:
        run.log("runner", f"Testing script v{version}: TMDB {body.type} {body.tmdb_id}"
                + (f" S{body.season}E{body.episode}" if body.season is not None else "") + f", timeout {timeout_s}s")
        try:
            run.log("runner", "fetching TMDB metadata...")
            meta = await build_meta(body.type, body.tmdb_id, body.season, body.episode)
        except TmdbNotFound:
            run.log("runner", "FAILED: TMDB id not found")
            return done({"ok": False, "context": None, "error": "TMDB id not found"})
        except TmdbError as e:
            run.log("runner", f"FAILED: TMDB error: {e}")
            return done({"ok": False, "context": None, "error": f"TMDB error: {e}"})
        run.log("runner", f"TMDB: {meta.get('title')} ({meta.get('year')}), imdb {meta.get('imdb_id')}")
        if uses_browser:
            run.log("runner", "browser source: waiting for a free browser slot...")
        async with source_slot(uses_browser):
            run.log("runner", "script started" + (" (verbose browser log on)" if body.verbose else ""))
            t0 = time.monotonic()
            ticker = asyncio.ensure_future(watch_memory())
            try:
                result = await run_script(code, meta, timeout_s, stats, on_log=run.feed,
                                          extra_env={"DEBUG": "pw:api"} if body.verbose else None)
            except ScriptError as e:
                run.log("runner", f"FAILED after {int((time.monotonic() - t0) * 1000)} ms: {str(e).strip().splitlines()[-1][:300] if str(e).strip() else e}")
                return done({"ok": False, "context": meta, "error": str(e)})
        n = len(result.get("streams") or []) if isinstance(result, dict) else 0
        run.log("runner", f"finished in {int((time.monotonic() - t0) * 1000)} ms: {n} stream(s), "
                f"{len((result or {}).get('subtitles') or [])} subtitle(s)" + (f", peak {stats['peak_mb']} MB" if stats.get("peak_mb") else ""))
        done({"ok": True, "context": meta, "result": result})
    except asyncio.CancelledError:
        run.log("runner", "stopped by you: script killed")
        done({"ok": False, "context": None, "error": "stopped"}, "cancelled")
        raise
    except Exception as e:                                  # never leave a test stuck on "running"
        run.log("runner", f"internal error: {e}")
        done({"ok": False, "context": None, "error": f"internal error: {e}"})
    finally:
        if ticker is not None:
            ticker.cancel()


@router.post("/sources/{source_id}/test/start", status_code=202)
async def start_test(source_id: int, body: TestIn, db: AsyncSession = Depends(get_db)):
    """Start a test run in the background and return its id; follow it with GET /admin/tests/{id}?after=<next>."""
    s = await get_source(db, source_id)
    sc = (next((x for x in s.scripts if x.version == body.version), None) if body.version
          else next((x for x in s.scripts if x.active), None))
    if sc is None:
        raise HTTPException(404, "no such script version (or no active script)")
    run = testlog.create(s.name, sc.version)
    run.task = asyncio.ensure_future(_run_test(run, sc.code, sc.version, s.timeout_s, bool(s.uses_browser), body))
    return {"test_id": run.id}


@router.get("/tests/{test_id}")
async def get_test(test_id: str, after: int = 0):
    """New log lines since `after` (use the `next` of the previous answer). `result` is filled once status != running."""
    run = testlog.get(test_id)
    if run is None:
        raise HTTPException(404, "test not found (tests are kept for one hour)")
    return run.since(after)


@router.post("/tests/{test_id}/cancel")
async def cancel_test(test_id: str):
    run = testlog.get(test_id)
    if run is None:
        raise HTTPException(404, "test not found")
    if run.status == "running" and run.task is not None:
        run.task.cancel()
    return {"status": run.status}


# ---------------- api keys ----------------
def key_dict(k: ApiKey) -> dict:
    return {"id": k.id, "name": k.name, "prefix": k.prefix, "enabled": k.enabled,
            "rate_limit_per_min": k.rate_limit_per_min, "created_at": k.created_at,
            "last_used_at": k.last_used_at}


@router.get("/api-keys")
async def list_keys(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(ApiKey).order_by(ApiKey.id))
    return [key_dict(k) for k in res.scalars()]


@router.post("/api-keys", status_code=201)
async def create_key(body: ApiKeyIn, db: AsyncSession = Depends(get_db)):
    plain = generate_key()
    k = ApiKey(name=body.name, key_hash=hash_key(plain), prefix=plain[:10],
               rate_limit_per_min=body.rate_limit_per_min)
    db.add(k)
    await db.commit()
    # The plaintext key is shown ONCE; only its hash is stored.
    return {**key_dict(k), "key": plain}


@router.patch("/api-keys/{key_id}")
async def update_key(key_id: int, body: ApiKeyUpdate, db: AsyncSession = Depends(get_db)):
    k = await db.get(ApiKey, key_id)
    if k is None:
        raise HTTPException(404, "api key not found")
    for name, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(k, name, v)
    await db.commit()
    return key_dict(k)


# ---------------- overview / tasks / library (for the admin UI) ----------------
@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db)):
    async def scalar(q):
        return (await db.execute(q)).scalar_one()

    lib = {st: n for st, n in (await db.execute(
        select(LibraryItem.status, func.count()).group_by(LibraryItem.status))).all()}
    used = await scalar(select(func.coalesce(func.sum(LibraryItem.size), 0))
                        .where(LibraryItem.status == "ready"))
    settings.library_path.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(settings.library_path)
    return {
        "sources_total": await scalar(select(func.count()).select_from(Source)),
        "sources_enabled": await scalar(select(func.count()).select_from(Source).where(Source.enabled.is_(True))),
        "api_keys": await scalar(select(func.count()).select_from(ApiKey)),
        "tasks": await scalar(select(func.count()).select_from(Task)),
        "library": lib,
        "library_bytes": int(used),
        "disk_free": du.free,
        "disk_total": du.total,
    }


@router.get("/tasks")
async def admin_tasks(limit: int = 50, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Task).order_by(Task.created_at.desc()).limit(min(limit, 200))
                           .options(selectinload(Task.runs).selectinload(SourceRun.streams)))
    return [{
        "id": t.id, "title": (t.meta or {}).get("title"), "type": t.media_type, "tmdb_id": t.tmdb_id,
        "season": t.season, "episode": t.episode, "status": t.status, "queue_position": queue.position(t.id),
        "created_at": t.created_at, "expires_at": t.expires_at,
        "runs": [{"source": r.source_name, "public_name": r.public_name, "starred": bool(r.starred), "status": r.status, "error": r.error,
                  "duration_ms": r.duration_ms, "peak_mb": r.peak_mb, "streams": len(r.streams)} for r in t.runs],
    } for t in res.scalars()]


def library_dict(i: LibraryItem) -> dict:
    speed, eta = speedmeter.read(i.id) if i.status == "downloading" else (None, None)
    return {"title": i.title, "tmdb_id": i.tmdb_id, "media_type": i.media_type, "season": i.season, "episode": i.episode,
            "subtitle_count": len(i.subtitles or []), "subtitles_status": i.subtitles_status or "none",
            "mode": i.mode or "file", "playable": bool(i.playable) or (i.status == "ready" and (i.mode or "file") == "file"),
            "download_speed": speed, "eta_seconds": eta,"id": i.id, "task_id": i.task_id, "source": i.source_name, "public_name": i.public_name,
            "quality": i.quality,
            "format": i.format, "status": i.status, "error": i.error, "size": i.size,
            "total_bytes": i.total_bytes, "ttl_hours": i.ttl_hours, "requested_at": i.requested_at,
            "ready_at": i.ready_at, "delete_at": i.delete_at}


@router.get("/library")
async def admin_library(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(LibraryItem).order_by(LibraryItem.requested_at.desc()).limit(200))
    return [library_dict(i) for i in res.scalars()]


@router.delete("/library/{item_id}", status_code=204)
async def admin_delete_library(item_id: str, db: AsyncSession = Depends(get_db)):
    item = await db.get(LibraryItem, item_id)
    if item is None:
        raise HTTPException(404, "library item not found")
    cancel_download(item.id)
    cancel_subtitles(item.id)
    remove_item_dir(item.id)
    item.status = "deleted"
    item.file_path = None
    item.playable = False
    item.subtitles = []
    item.subtitles_status = "none"
    await db.commit()


# ---------------- example scripts (the UI offers them as starting points) ----------------
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


@router.get("/examples")
async def list_examples():
    if not EXAMPLES_DIR.is_dir():
        return []
    return [{"name": p.stem, "code": p.read_text(encoding="utf-8")} for p in sorted(EXAMPLES_DIR.glob("*.py"))]
