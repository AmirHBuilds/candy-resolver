from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..fileutil import CONTENT_TYPES
from ..models import LibraryItem, as_utc, utcnow
from ..rangeutil import RangeNotSatisfiable, parse_range
from ..signing import verify

router = APIRouter(tags=["files"])


def _iter_file(path: Path, start: int, length: int, chunk: int = 1024 * 1024):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


@router.api_route("/f/{item_id}/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_file(item_id: str, filename: str, request: Request,
                     exp: str = "", sig: str = "", db: AsyncSession = Depends(get_db)):
    """Serves a library file. Needs a valid signed, expiring link; supports Range so seeking works."""
    if not verify(item_id, exp, sig):
        raise HTTPException(403, "invalid or expired link")
    item = await db.get(LibraryItem, item_id)
    if (item is None or (item.mode or "file") != "file" or item.status != "ready" or not item.file_path
            or as_utc(item.delete_at) <= utcnow()):
        raise HTTPException(404, "file not available")
    path = Path(item.file_path)
    if not path.is_file():
        raise HTTPException(404, "file not available")

    size = path.stat().st_size
    ctype = CONTENT_TYPES.get(path.suffix.lstrip("."), "application/octet-stream")
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}

    try:
        rng = parse_range(request.headers["range"], size) if "range" in request.headers else None
    except RangeNotSatisfiable:
        raise HTTPException(416, "range not satisfiable", headers={"Content-Range": f"bytes */{size}"})

    if rng:
        start, end = rng
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    else:
        start, end, status = 0, size - 1, 200
    length = end - start + 1
    headers["Content-Length"] = str(length)

    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=ctype)
    return StreamingResponse(_iter_file(path, start, length), status_code=status,
                             headers=headers, media_type=ctype)
