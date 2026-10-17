"""Admin panel extras: system monitor, live logs, disk file browser, processes, maintenance, settings.
Everything here needs the admin token (same as the rest of /admin)."""
import asyncio
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import autoclean, diskclean, diskjob, logbuf, scheduler, sysmon
from ..hostclean import hostclean
from ..auth import require_admin
from ..config import settings
from ..db import get_db
from ..downloader import cancel_download, downloads
from ..itemfiles import item_dir, remove_item_dir
from ..library import expire_library, item_to_out, sweep_orphans
from ..models import LibraryItem
from ..resolver import expire_task_results, purge_old_tasks
from ..subs_job import cancel_subtitles

router = APIRouter(prefix="/admin", tags=["admin-panel"], dependencies=[Depends(require_admin)])

_ID = re.compile(r"lib_[0-9a-f]{16}")
ACTIVE = ("queued", "downloading", "ready")


# ---------------------------------------------------------------- system
@router.get("/system")
async def system():
    snap = await asyncio.to_thread(sysmon.snapshot)
    snap["logs"] = logbuf.counts()
    snap["downloads_running"] = sum(1 for t in downloads.values() if not t.done())
    snap["queue"] = scheduler.stats()
    return snap


@router.get("/system/history")
async def system_history(n: int = Query(120, ge=1, le=360)):
    return {"every_s": sysmon.SAMPLE_EVERY, "points": sysmon.history(n)}


@router.get("/checks")
async def checks():
    return await asyncio.to_thread(sysmon.tool_checks)


@router.get("/processes")
async def processes():
    return await asyncio.to_thread(sysmon.list_processes)


@router.delete("/processes/{pid}")
async def kill_process(pid: int):
    try:
        name = await asyncio.to_thread(sysmon.kill_process, pid)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    logbuf.add("WARNING", "admin", f"killed process {pid} ({name}) from the panel")
    return {"killed": pid, "name": name}


# ---------------------------------------------------------------- logs
@router.get("/logs")
async def logs(level: str = "DEBUG", q: str = "", since_id: int = 0, limit: int = 300):
    return logbuf.query(level, q, since_id, limit)


@router.post("/logs/clear", status_code=204)
async def logs_clear():
    logbuf.clear()


# ---------------------------------------------------------------- files on disk
def _scan_dir(path: str) -> tuple[int, int, float]:
    """(bytes, file count, newest mtime) of everything under path. Symlinks are never followed."""
    total = count = 0
    newest = 0.0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            st = e.stat(follow_symlinks=False)
                            total += st.st_size
                            count += 1
                            newest = max(newest, st.st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count, newest


def _scan_library() -> list[dict]:
    base = settings.library_path
    base.mkdir(parents=True, exist_ok=True)
    out = []
    with os.scandir(base) as it:
        for e in it:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                b, n, m = _scan_dir(e.path)
                out.append({"name": e.name, "kind": "dir", "bytes": b, "files": n, "mtime": m})
            else:
                try:
                    st = e.stat(follow_symlinks=False)
                    out.append({"name": e.name, "kind": "file", "bytes": st.st_size, "files": 1, "mtime": st.st_mtime})
                except OSError:
                    pass
    return out


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


@router.get("/files")
async def files(db: AsyncSession = Depends(get_db)):
    """Every folder in the library on disk, matched with its database row. `orphan` = nothing alive points at it."""
    entries = await asyncio.to_thread(_scan_library)
    ids = [e["name"] for e in entries if e["kind"] == "dir" and _ID.fullmatch(e["name"])]
    rows = {}
    if ids:
        res = await db.execute(select(LibraryItem).where(LibraryItem.id.in_(ids)))
        rows = {r.id: r for r in res.scalars()}
    items = []
    for e in entries:
        r = rows.get(e["name"])
        orphan = (r is None) or r.status not in ACTIVE
        items.append({
            "id": e["name"], "kind": e["kind"], "bytes": e["bytes"], "files": e["files"], "modified": _iso(e["mtime"]),
            "known": r is not None, "orphan": orphan and e["kind"] == "dir",
            "status": r.status if r else None, "mode": r.mode if r else None, "title": r.title if r else None,
            "season": r.season if r else None, "episode": r.episode if r else None,
            "quality": r.quality if r else None, "source": r.source_name if r else None,
            "delete_at": r.delete_at if r else None, "downloading": e["name"] in downloads and not downloads[e["name"]].done(),
        })
    items.sort(key=lambda x: x["bytes"], reverse=True)
    du = shutil.disk_usage(settings.library_path)
    return {
        "root": str(settings.library_path), "total_bytes": sum(i["bytes"] for i in items),
        "orphan_bytes": sum(i["bytes"] for i in items if i["orphan"]), "orphans": sum(1 for i in items if i["orphan"]),
        "disk_free": du.free, "disk_total": du.total, "items": items,
    }


@router.get("/files/{item_id}")
async def file_list(item_id: str):
    """The individual files inside one library folder (video, playlist, segments, subs)."""
    if not _ID.fullmatch(item_id):
        raise HTTPException(400, "bad id")
    root = item_dir(item_id)
    if not root.is_dir():
        raise HTTPException(404, "folder not found")

    def walk():
        out = []
        for p in root.rglob("*"):
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
                out.append({"path": str(p.relative_to(root)), "bytes": st.st_size, "modified": _iso(st.st_mtime)})
            except OSError:
                continue
        return out

    all_files = await asyncio.to_thread(walk)
    all_files.sort(key=lambda f: f["bytes"], reverse=True)
    return {"id": item_id, "count": len(all_files), "bytes": sum(f["bytes"] for f in all_files), "files": all_files[:300]}


@router.get("/files/{item_id}/link")
async def file_link(item_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """A short-lived signed link so you can open / download / play the file from the panel."""
    item = await db.get(LibraryItem, item_id)
    if item is None or item.status not in ("ready", "downloading"):
        raise HTTPException(404, "no playable file for this id")
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    out = item_to_out(item, base)
    url = out.url or out.stream_url
    if not url:
        raise HTTPException(409, "not playable yet")
    return {"url": url, "kind": "hls" if out.stream_url else "file"}


async def _delete_folder(db: AsyncSession, item_id: str) -> None:
    """Delete one folder. If it belongs to a live item, behave exactly like the normal delete (cancel + mark deleted)."""
    cancel_download(item_id)
    cancel_subtitles(item_id)
    remove_item_dir(item_id)
    item = await db.get(LibraryItem, item_id)
    if item is not None and item.status in (*ACTIVE, "failed"):
        item.status = "deleted"
        item.file_path = None
        item.playable = False
        item.subtitles = []
        item.subtitles_status = "none"


@router.delete("/files/{item_id}")
async def file_delete(item_id: str, db: AsyncSession = Depends(get_db)):
    if not _ID.fullmatch(item_id):
        raise HTTPException(400, "bad id")
    if not item_dir(item_id).exists() and (await db.get(LibraryItem, item_id)) is None:
        raise HTTPException(404, "folder not found")
    await _delete_folder(db, item_id)
    await db.commit()
    logbuf.add("INFO", "admin", f"deleted {item_id} from disk (panel)")
    return {"deleted": item_id}


class CleanupIn(BaseModel):
    what: str          # orphans | everything


@router.post("/files/cleanup")
async def files_cleanup(body: CleanupIn, db: AsyncSession = Depends(get_db)):
    """orphans: folders nothing alive points at (crash leftovers, failed or expired items).
    everything: cancel all downloads and delete EVERY folder in the library."""
    if body.what not in ("orphans", "everything"):
        raise HTTPException(422, "what must be 'orphans' or 'everything'")
    entries = await asyncio.to_thread(_scan_library)
    ids = [e["name"] for e in entries if e["kind"] == "dir" and _ID.fullmatch(e["name"])]
    rows = {}
    if ids:
        res = await db.execute(select(LibraryItem).where(LibraryItem.id.in_(ids)))
        rows = {r.id: r for r in res.scalars()}
    removed = freed = 0
    for e in entries:
        if e["kind"] != "dir" or not _ID.fullmatch(e["name"]):
            continue
        r = rows.get(e["name"])
        if body.what == "orphans" and r is not None and r.status in ACTIVE:
            continue
        await _delete_folder(db, e["name"])
        removed += 1
        freed += e["bytes"]
    await db.commit()
    logbuf.add("WARNING", "admin", f"cleanup '{body.what}': removed {removed} folders, freed {freed} bytes")
    return {"removed": removed, "freed_bytes": freed}


# ---------------------------------------------------------------- disk cleaner
@router.post("/disk/scan")
async def disk_scan(host: bool = Query(False, description="also ask the host agent for a dry run on the VPS itself"), db: AsyncSession = Depends(get_db)):
    """What the disk cleaner WOULD remove. Deletes nothing."""
    return {"disk": diskjob.disk_info(), **diskclean.summarize(await diskjob.candidates(db)),
            "host": hostclean.request(dry_run=True) if host else None}


class DiskCleanIn(BaseModel):
    database: bool = True      # also: expire files and links that are due, purge old tasks, forget finished library rows
    host: bool = False         # also ask the host agent to clean the VPS itself
    host_aggressive: bool = False   # ... including ALL unused Docker images older than 7 days


@router.post("/disk/clean")
async def disk_clean(body: DiskCleanIn):
    """Clean the disk now: leftovers nobody needs (orphan library folders, old script/browser temp, stale locks, package caches).
    Running downloads and fresh files are never touched. With database=true it first expires what is due."""
    try:
        return await diskjob.run(body.database, "manual", body.host, body.host_aggressive)
    except diskjob.Busy:
        raise HTTPException(409, "a disk clean is already running")


class AutoCleanIn(BaseModel):
    enabled: bool | None = None
    every_hours: int | None = Field(default=None, ge=1, le=168)
    database: bool | None = None
    emergency: bool | None = None      # also clean (at most every 15 min) while the disk is nearly full
    host: bool | None = None           # the automatic clean also asks the host agent to clean the VPS
    host_aggressive: bool | None = None


@router.get("/hostclean")
async def get_hostclean():
    """The host agent: installed / alive, a pending or running request, and the result of its last run (with a log)."""
    return hostclean.status()


class HostCleanIn(BaseModel):
    aggressive: bool = False
    dry_run: bool = False


@router.post("/hostclean/run")
async def run_hostclean(body: HostCleanIn):
    """Ask the host agent to clean the VPS now (dry_run = only report what it would do). Poll GET /admin/hostclean for the result."""
    return hostclean.request(body.aggressive, body.dry_run)


@router.get("/autoclean")
async def get_autoclean():
    """Settings of the automatic disk clean, the result of its last run and when the next one is due."""
    return autoclean.state.public(time.time(), diskjob.disk_info())


@router.put("/autoclean")
async def put_autoclean(body: AutoCleanIn):
    autoclean.state.update(body.model_dump(exclude_unset=True))
    return autoclean.state.public(time.time(), diskjob.disk_info())


# ---------------------------------------------------------------- maintenance
@router.post("/maintenance/{job}")
async def maintenance(job: str, db: AsyncSession = Depends(get_db)):
    if job == "expire_library":
        n = await expire_library()
    elif job == "expire_tasks":
        n = await expire_task_results()
    elif job == "purge_tasks":
        n = await purge_old_tasks()
    elif job == "sweep_orphans":
        await sweep_orphans()
        n = None
    elif job == "forget_finished":     # only rows that are finished with (failed / expired / deleted); their files are already gone
        res = await db.execute(delete(LibraryItem).where(LibraryItem.status.in_(("failed", "expired", "deleted"))))
        await db.commit()
        n = res.rowcount
    elif job == "clear_history":       # EVERYTHING: every entry of every kind (videos, HLS, subtitle packs), their files, running jobs
        ids = list((await db.execute(select(LibraryItem.id))).scalars())
        for item_id in ids:
            cancel_download(item_id)
            cancel_subtitles(item_id)
        if ids:
            await asyncio.sleep(0.3)                 # let the cancelled jobs finish before their rows disappear
        for item_id in ids:
            remove_item_dir(item_id)
        res = await db.execute(delete(LibraryItem))
        await db.commit()
        n = res.rowcount
        await sweep_orphans()                        # any folder that was left without a row
    elif job == "cancel_downloads":
        n = 0
        for item_id in list(downloads):
            if cancel_download(item_id):
                n += 1
    else:
        raise HTTPException(404, "unknown job")
    logbuf.add("INFO", "admin", f"maintenance '{job}' ran" + (f" ({n} affected)" if n is not None else ""))
    return {"job": job, "affected": n}


# ---------------------------------------------------------------- settings (read-only)
_SECRET = {"admin_token", "signing_secret", "tmdb_api_key", "telegram_bot_token"}


@router.get("/settings")
async def get_settings():
    out = []
    for k, v in settings.model_dump().items():
        if k in _SECRET:
            v = "set" if v else "empty"
        elif k == "database_url":
            v = re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", str(v))
        out.append({"key": k, "env": k.upper(), "value": v})
    return out


# ---------------------------------------------------------------- numbers for the overview
@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    """Small extra numbers for the overview: activity in the last 24 h."""
    since = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=24)
    res = await db.execute(select(LibraryItem.status, func.count()).where(LibraryItem.requested_at >= since)
                           .group_by(LibraryItem.status))
    by_status = {s: n for s, n in res.all()}
    return {"library_24h": by_status, "now": time.time()}
