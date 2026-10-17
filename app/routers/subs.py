"""GET /s/{item_id}/{n}.vtt : a library item's subtitles (WebVTT) for a player on another domain.
All decisions are made in subserve.py; this only builds the response."""
from fastapi import APIRouter, Request, Response

from ..config import settings
from ..db import SessionLocal
from ..hlsserve import ITEM_ID, HlsReply, precheck, preflight
from ..models import LibraryItem
from ..subserve import handle_sub_request, valid_sub_filename

router = APIRouter(tags=["subtitles"])


def _respond(reply: HlsReply) -> Response:
    if reply.status >= 400:
        return Response(content=reply.detail, status_code=reply.status, headers=reply.headers, media_type="text/plain")
    return Response(content=reply.body or b"", status_code=reply.status, headers=reply.headers)


@router.api_route("/s/{item_id}/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_subtitle(item_id: str, filename: str, request: Request, exp: str = "", sig: str = ""):
    origin = request.headers.get("origin")
    bad = precheck(item_id, filename, exp, sig, origin, settings.cors_origins, valid_name=valid_sub_filename)
    if bad:                                              # no DB hit for bad requests
        return _respond(bad)
    item = None
    if ITEM_ID.fullmatch(item_id):
        async with SessionLocal() as db:
            item = await db.get(LibraryItem, item_id)
    return _respond(handle_sub_request(item=item, item_id=item_id, base_dir=settings.library_path, filename=filename,
                                       exp=exp, sig=sig, method=request.method, origin=origin,
                                       allowed_origins=settings.cors_origins))


@router.options("/s/{item_id}/{filename}", include_in_schema=False)
async def subtitle_preflight(item_id: str, filename: str, request: Request):
    return _respond(preflight(request.headers.get("origin"), settings.cors_origins))
