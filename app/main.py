import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

from . import models  # noqa: F401  (registers tables)
from .config import ensure_secrets, settings
from .db import Base, engine
from .migrate import migrate
from .browserflag import backfill_browser_flags
from .library import backfill_library_info, expire_library, fail_stuck_library, sweep_orphans
from .resolver import expire_task_results, fail_stuck_tasks, purge_old_tasks
from . import autoclean, diskjob, logbuf, reportjob, sysmon
from .live_http import fetcher as live_fetcher
from .notify import notifier, watch_disk
from .version import VERSION
from .routers import admin, admin_panel, admin_telegram, files, hls, library, live, resolve, subs


async def _cleanup_loop():
    while True:
        for job in (expire_library, expire_task_results, purge_old_tasks):
            try:
                await job()
            except Exception:  # keep the loop alive
                logging.getLogger("candy.cleanup").exception("cleanup error in %s", job.__name__)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_secrets()                 # (prints the generated admin token: log capture starts AFTER it on purpose)
    logbuf.install()
    await notifier.start()
    settings.library_path.mkdir(parents=True, exist_ok=True)
    # Phase 1-2: create tables on startup. Switch to Alembic migrations before the schema changes.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate(engine)  # adds columns introduced after your database was first created
    await backfill_browser_flags()
    await fail_stuck_tasks()
    await backfill_library_info()
    await fail_stuck_library()
    await sweep_orphans()
    cleanup = asyncio.create_task(_cleanup_loop())
    disk_watch = asyncio.create_task(watch_disk(settings.library_path))
    autoclean.state.load()
    daily_report = asyncio.create_task(reportjob.loop())
    auto_clean = asyncio.create_task(autoclean.run_loop(autoclean.state, diskjob.automatic, diskjob.disk_free_total))
    sysmon.start()
    notifier.emit("system", "Server started", {"Version": app.version}, level="info", key="start", dedupe_s=0)
    yield
    notifier.emit("system", "Server stopping", {}, level="info", key="stop", dedupe_s=0)
    cleanup.cancel()
    disk_watch.cancel()
    auto_clean.cancel()
    daily_report.cancel()
    sysmon.stop()
    await notifier.stop()
    await live_fetcher.aclose()


app = FastAPI(title="candyresolver", version=VERSION, lifespan=lifespan)
app.include_router(resolve.router)
app.include_router(library.router)
app.include_router(files.router)
app.include_router(hls.router)
app.include_router(live.router)
app.include_router(live.admin_router)
app.include_router(subs.router)
app.include_router(admin.router)
app.include_router(admin_panel.router)
app.include_router(admin_telegram.router)


@app.get("/health")
async def health():
    return {"ok": True}


STATIC = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/ui")


@app.get("/ui", include_in_schema=False)
async def admin_ui():
    """Admin web UI (single page). The page itself is public; every action needs the admin token."""
    return FileResponse(STATIC / "admin.html", headers={
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                   "script-src 'self' 'unsafe-inline'; connect-src 'self'",
    })
