"""Decides what to answer for /h/{item_id}/{filename}. Pure logic (files + signature + CORS), no web framework,
so the security rules can be tested directly. routers/hls.py only turns the reply into an HTTP response."""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import hls
from .rangeutil import RangeNotSatisfiable, parse_range
from .signing import verify

ITEM_ID = re.compile(r"lib_[0-9a-f]{16}")
_EXP = re.compile(r"[0-9]{1,12}")
_SIG = re.compile(r"[0-9a-f]{64}")
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
SEGMENT_TYPE = "video/mp2t"


@dataclass
class HlsReply:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    file: tuple[Path, int, int] | None = None     # (path, start, length) to stream
    detail: str = ""


def cors_headers(origin: str | None, allowed: str) -> dict:
    """allowed = "" (no cross-origin access), "*", or a comma-separated list of origins."""
    allow = [o.strip().rstrip("/") for o in (allowed or "").split(",") if o.strip()]
    if not allow:
        return {}
    if "*" in allow:
        h = {"Access-Control-Allow-Origin": "*"}
    elif origin and origin.rstrip("/") in allow:
        h = {"Access-Control-Allow-Origin": origin.rstrip("/"), "Vary": "Origin"}
    else:
        return {"Vary": "Origin"}
    h.update({"Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
              "Access-Control-Allow-Headers": "Range",
              "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
              "Access-Control-Max-Age": "86400"})
    return h


def preflight(origin: str | None, allowed: str) -> HlsReply:
    return HlsReply(204, cors_headers(origin, allowed))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def precheck(item_id: str, filename: str, exp: str, sig: str, origin: str | None = None,
             allowed: str = "") -> HlsReply | None:
    """Cheap checks that need no database: strict names, then the signature. None = looks fine."""
    cors = {**cors_headers(origin, allowed), "Cache-Control": "no-store"}
    if not ITEM_ID.fullmatch(item_id or "") or not hls.valid_hls_filename(filename or ""):
        return HlsReply(404, cors, detail="not found")
    if not _EXP.fullmatch(exp or "") or not _SIG.fullmatch(sig or "") or not verify(item_id, exp, sig):
        return HlsReply(403, cors, detail="invalid or expired link")
    return None


def handle_hls_request(*, item, item_id: str, base_dir, filename: str, exp: str, sig: str, method: str = "GET",
                       range_header: str | None = None, origin: str | None = None, allowed_origins: str = "",
                       now: datetime | None = None) -> HlsReply:
    bad = precheck(item_id, filename, exp, sig, origin, allowed_origins)
    if bad:
        return bad
    cors = cors_headers(origin, allowed_origins)
    miss = HlsReply(404, {**cors, "Cache-Control": "no-store"}, detail="not available")
    now = now or datetime.now(timezone.utc)

    if item is None or getattr(item, "id", None) != item_id or getattr(item, "mode", "file") != "hls":
        return miss
    if item.status == "ready":
        if item.delete_at is None or _aware(item.delete_at) <= now:
            return miss
    elif item.status == "downloading":
        if not item.playable:
            return miss
    else:                                    # queued / failed / expired / deleted: nothing to serve
        return miss

    folder = Path(base_dir) / item_id
    try:
        real = (folder / filename).resolve(strict=True)
        if real.parent != folder.resolve() or not real.is_file():
            return miss
    except (FileNotFoundError, OSError):
        return miss

    downloading = item.status == "downloading"
    if filename == hls.PLAYLIST_NAME:
        text = real.read_text(encoding="utf-8", errors="replace")
        body = hls.rewrite_playlist(text, f"exp={exp}&sig={sig}", lambda n: (folder / n).is_file()).encode()
        headers = {**cors, "Content-Type": PLAYLIST_TYPE, "Content-Length": str(len(body)),
                   # the growing playlist must never be cached; the finished one may be, briefly
                   "Cache-Control": "no-store" if downloading else "private, max-age=60"}
        return HlsReply(200, headers, body=None if method == "HEAD" else body)

    size = real.stat().st_size
    try:
        rng = parse_range(range_header, size) if range_header else None
    except RangeNotSatisfiable:
        return HlsReply(416, {**cors, "Content-Range": f"bytes */{size}", "Cache-Control": "no-store"},
                        detail="range not satisfiable")
    headers = {**cors, "Content-Type": SEGMENT_TYPE, "Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if rng:
        start, end, status = rng[0], rng[1], 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    else:
        start, end, status = 0, size - 1, 200
    length = end - start + 1
    headers["Content-Length"] = str(length)
    return HlsReply(status, headers, file=None if method == "HEAD" else (real, start, length))
