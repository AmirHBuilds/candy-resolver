import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

from . import models  # noqa: F401  (registers tables)
from .config import ensure_secrets, settings
from .db import Base, engine
from .migrate import migrate
from .library import expire_library, fail_stuck_library, sweep_orphans
from .resolver import fail_stuck_tasks, purge_expired
from .routers import admin, files, library, resolve


async def _cleanup_loop():
    while True:
        for job in (expire_library, purge_expired):
            try:
                await job()
            except Exception as e:  # keep the loop alive
                print(f"cleanup error in {job.__name__}:", e)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_secrets()
    settings.library_path.mkdir(parents=True, exist_ok=True)
    # Phase 1-2: create tables on startup. Switch to Alembic migrations before the schema changes.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate(engine)  # adds columns introduced after your database was first created
    await fail_stuck_tasks()
    await fail_stuck_library()
    await sweep_orphans()
    cleanup = asyncio.create_task(_cleanup_loop())
    yield
    cleanup.cancel()


app = FastAPI(title="candyresolver", version="0.2.0", lifespan=lifespan)
app.include_router(resolve.router)
app.include_router(library.router)
app.include_router(files.router)
app.include_router(admin.router)


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
