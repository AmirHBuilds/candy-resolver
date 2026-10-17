"""GET /h/{item_id}/index.m3u8 and /h/{item_id}/seg_00000.ts : progressive HLS items, for a player on another domain.
All decisions are made in hlsserve.py (strict names, signature, item state, CORS); this only builds the response."""
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from ..config import settings
from ..db import SessionLocal
from ..hlsserve import HlsReply, ITEM_ID, handle_hls_request, precheck, preflight
from ..models import LibraryItem
from .files import _iter_file

router = APIRouter(tags=["hls"])


def _respond(reply: HlsReply, method: str) -> Response:
    if reply.file and method != "HEAD":
        path, start, length = reply.file
        return StreamingResponse(_iter_file(path, start, length), status_code=reply.status, headers=reply.headers)
    if reply.status >= 400:
        return Response(content=reply.detail, status_code=reply.status, headers=reply.headers, media_type="text/plain")
    return Response(content=reply.body or b"", status_code=reply.status, headers=reply.headers)


@router.api_route("/h/{item_id}/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_hls(item_id: str, filename: str, request: Request, exp: str = "", sig: str = ""):
    origin = request.headers.get("origin")
    bad = precheck(item_id, filename, exp, sig, origin, settings.cors_origins)   # no DB hit for bad requests
    if bad:
        return _respond(bad, request.method)
    item = None
    if ITEM_ID.fullmatch(item_id):
        async with SessionLocal() as db:      # short session: segments are requested very often
            item = await db.get(LibraryItem, item_id)
    reply = handle_hls_request(
        item=item, item_id=item_id, base_dir=settings.library_path, filename=filename, exp=exp, sig=sig,
        method=request.method, range_header=request.headers.get("range"), origin=origin,
        allowed_origins=settings.cors_origins)
    return _respond(reply, request.method)


@router.options("/h/{item_id}/{filename}", include_in_schema=False)
async def hls_preflight(item_id: str, filename: str, request: Request):
    return _respond(preflight(request.headers.get("origin"), settings.cors_origins), "OPTIONS")
