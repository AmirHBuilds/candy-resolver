import ast
import shutil
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..auth import generate_key, hash_key, require_admin
from ..config import settings
from ..db import get_db
from ..downloader import cancel_download
from ..models import ApiKey, LibraryItem, Script, Source, SourceRun, Task
from ..runner import ScriptError, run_script
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


def source_dict(s: Source) -> dict:
    active = next((sc.version for sc in s.scripts if sc.active), None)
    return {"id": s.id, "name": s.name, "base_url": s.base_url, "language": s.language,
            "enabled": s.enabled, "timeout_s": s.timeout_s, "active_version": active,
            "created_at": s.created_at}


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
    return [source_dict(s) for s in res.scalars()]


@router.post("/sources", status_code=201)
async def create_source(body: SourceIn, db: AsyncSession = Depends(get_db)):
    s = Source(**body.model_dump())
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
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(s, k, v)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "a source with this name already exists")
    return source_dict(s)


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
    await db.commit()
    return {"version": version, "active": body.activate}


@router.post("/sources/{source_id}/scripts/{version}/activate")
async def activate_script(source_id: int, version: int, db: AsyncSession = Depends(get_db)):
    s = await get_source(db, source_id)
    target = next((x for x in s.scripts if x.version == version), None)
    if target is None:
        raise HTTPException(404, "script version not found")
    for x in s.scripts:
        x.active = x is target
    await db.commit()
    return {"active_version": version}


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
    t0 = time.monotonic()
    try:
        result = await run_script(sc.code, meta, s.timeout_s)
        return {"ok": True, "version": sc.version, "duration_ms": int((time.monotonic() - t0) * 1000),
                "context": meta, "result": result}
    except ScriptError as e:
        return {"ok": False, "version": sc.version, "duration_ms": int((time.monotonic() - t0) * 1000),
                "context": meta, "error": str(e)}


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
        "season": t.season, "episode": t.episode, "status": t.status,
        "created_at": t.created_at, "expires_at": t.expires_at,
        "runs": [{"source": r.source_name, "status": r.status, "error": r.error,
                  "duration_ms": r.duration_ms, "streams": len(r.streams)} for r in t.runs],
    } for t in res.scalars()]


def library_dict(i: LibraryItem) -> dict:
    return {"id": i.id, "task_id": i.task_id, "source": i.source_name, "quality": i.quality,
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
    shutil.rmtree(settings.library_path / item.id, ignore_errors=True)
    item.status = "deleted"
    item.file_path = None
    await db.commit()
