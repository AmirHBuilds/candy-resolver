"""The disk clean job: used by the admin button (Files on disk -> Clean disk) and by the automatic schedule."""
import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import diskclean, logbuf
from .autoclean import state
from .config import settings
from .db import SessionLocal
from .hostclean import hostclean
from .library import expire_library
from .models import LibraryItem
from .notify import notifier
from .resolver import expire_task_results, purge_old_tasks

ACTIVE = ("queued", "downloading", "ready")
_lock = asyncio.Lock()
_bg: set = set()                      # keeps background watchers alive


class Busy(Exception):
    pass


def cache_dirs() -> list[Path]:
    return [Path.home() / ".cache" / "pip", Path("/root/.cache/pip"), Path("/var/cache/apt/archives"), Path("/var/lib/apt/lists")]


def roots() -> list[Path]:
    return [settings.library_path, Path(tempfile.gettempdir()), *cache_dirs()]


def disk_info() -> dict:
    d = shutil.disk_usage(settings.library_path if settings.library_path.exists() else settings.library_path.parent)
    return {"total": d.total, "used": d.used, "free": d.free}


def disk_free_total() -> tuple[int, int]:
    d = disk_info()
    return d["free"], d["total"]


async def candidates(db: AsyncSession) -> list:
    active = set((await db.execute(select(LibraryItem.id).where(LibraryItem.status.in_(ACTIVE)))).scalars())
    settings.library_path.mkdir(parents=True, exist_ok=True)
    return await asyncio.to_thread(diskclean.scan, settings.library_path, active, tempfile.gettempdir(), None, 1800, cache_dirs())


async def run(database: bool, trigger: str = "manual", host: bool = False, host_aggressive: bool = False) -> dict:
    """Tidy the database (optional: expire what is due, forget finished rows, purge old tasks), then delete the disk leftovers.
    Running downloads, fresh files, symlinks and the data folder are never touched. Raises Busy if a clean is already running.
    host=True also asks the host agent to clean the VPS itself (Docker leftovers, system logs, apt, temp...): the answer is in out["host"]
    and the result shows up later in hostclean.status()."""
    if _lock.locked():
        raise Busy()
    async with _lock:
        t0, before, tidy = time.monotonic(), disk_info(), {}
        async with SessionLocal() as db:
            if database:
                tidy["expired_library_items"] = await expire_library()
                tidy["expired_task_results"] = await expire_task_results()
                tidy["purged_tasks"] = await purge_old_tasks()
                res = await db.execute(delete(LibraryItem).where(LibraryItem.status.in_(("failed", "expired", "deleted"))))
                await db.commit()
                tidy["forgotten_library_rows"] = res.rowcount
            cands = await candidates(db)
        report = await asyncio.to_thread(diskclean.delete, cands, roots())
        out = {"before": before, "after": disk_info(), "database": tidy, "trigger": trigger,
               "duration_s": round(time.monotonic() - t0, 1), **report,
               "host": hostclean.request(aggressive=host_aggressive) if host else None}
        msg = (f"disk clean ({trigger}): removed {report['removed']} item(s), freed {report['freed_bytes']} bytes"
               + (f", database tidy {tidy}" if tidy else "") + (f", {report['errors']} error(s)" if report["errors"] else "")
               + (", host clean requested" if out["host"] and out["host"].get("ok") else f", host clean NOT requested ({out['host']['reason']})" if out["host"] else ""))
        if trigger == "manual":
            logbuf.add("WARNING", "admin", msg)                 # (also reaches the Telegram "Admin actions" topic)
        else:
            logbuf.add("INFO", "autoclean", msg)
        state.record(trigger, out)
        return out


async def _watch_host(req_id: str, reason: str) -> None:
    """Wait (up to 30 min) for the host agent's answer to our request and tell Telegram if it matters."""
    try:
        for _ in range(90):
            await asyncio.sleep(20)
            last = hostclean.status().get("last")
            if last and last.get("id") == req_id:
                freed = (last.get("freed_kb") or 0) * 1024
                if freed >= 100 * 1024 * 1024 or not last.get("ok") or reason == "low disk":
                    notifier.emit("system", f"Host cleanup ({reason})",
                                  {"Freed": f"{freed / 1e6:.0f} MB", "Free space now": f"{(last.get('free_kb') or 0) * 1024 / 1e9:.1f} GB",
                                   "Result": None if last.get("ok") else "failed: see the Files on disk page"},
                                  level="info" if last.get("ok") else "warning", key=f"hostclean:{req_id}", dedupe_s=0)
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def automatic(reason: str) -> None:
    """Called by the schedule. Tells Telegram only when it matters: something big was freed, errors, or the disk was nearly full."""
    try:
        out = await run(state.cfg["database"], reason, state.cfg["host"], state.cfg["host_aggressive"])
    except Busy:
        return
    if out["host"] and out["host"].get("ok") and out["host"].get("id"):
        t = asyncio.create_task(_watch_host(out["host"]["id"], reason))
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    big = out["freed_bytes"] >= 100 * 1024 * 1024
    if big or out["errors"] or reason == "low disk":
        gb = lambda n: f"{n / 1e9:.1f} GB"
        notifier.emit("system", f"Automatic cleanup ({reason})",
                      {"Freed": f"{out['freed_bytes'] / 1e6:.0f} MB", "Items removed": out["removed"], "Free space now": gb(out["after"]["free"]),
                       "Errors": out["errors"] or None}, level="warning" if out["errors"] or reason == "low disk" else "info",
                      key=f"autoclean:{reason}", dedupe_s=3600)
