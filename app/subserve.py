"""Decides what to answer for /s/{item_id}/{n}.vtt (same signature + CORS rules as the video routes)."""
import re
from datetime import datetime, timezone
from pathlib import Path

from .hlsserve import HlsReply, _aware, cors_headers, precheck

SUB_NAME = re.compile(r"[0-9]{1,3}\.vtt")
VTT_TYPE = "text/vtt; charset=utf-8"


def valid_sub_filename(name: str) -> bool:
    return bool(SUB_NAME.fullmatch(name or ""))


def handle_sub_request(*, item, item_id: str, base_dir, filename: str, exp: str, sig: str, method: str = "GET",
                       origin: str | None = None, allowed_origins: str = "", now: datetime | None = None) -> HlsReply:
    bad = precheck(item_id, filename, exp, sig, origin, allowed_origins, valid_name=valid_sub_filename)
    if bad:
        return bad
    cors = cors_headers(origin, allowed_origins)
    miss = HlsReply(404, {**cors, "Cache-Control": "no-store"}, detail="not available")
    now = now or datetime.now(timezone.utc)

    if item is None or getattr(item, "id", None) != item_id:
        return miss
    if item.status == "ready":
        if item.delete_at is None or _aware(item.delete_at) <= now:
            return miss
    elif item.status not in ("queued", "downloading"):      # failed / expired / deleted: gone
        return miss

    subs = (Path(base_dir) / item_id / "subs")
    try:
        real = (subs / filename).resolve(strict=True)
        if real.parent != subs.resolve() or not real.is_file():
            return miss
        body = real.read_bytes()
    except (FileNotFoundError, OSError):
        return miss
    headers = {**cors, "Content-Type": VTT_TYPE, "Content-Length": str(len(body)),
               "Cache-Control": "private, max-age=3600"}           # a finished file never changes
    return HlsReply(200, headers, body=None if method == "HEAD" else body)
