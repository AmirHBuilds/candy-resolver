from pathlib import Path
from urllib.parse import urlparse

EXTS = {"mp4", "mkv", "webm", "m4v", "mov"}
CONTENT_TYPES = {"mp4": "video/mp4", "m4v": "video/mp4", "mkv": "video/x-matroska",
                 "webm": "video/webm", "mov": "video/quicktime"}


def pick_ext(fmt: str | None, url: str) -> str:
    f = (fmt or "").lower().lstrip(".")
    if f in EXTS:
        return f
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in EXTS else "mp4"


def is_hls(fmt: str | None, url: str) -> bool:
    return (fmt or "").lower() in ("hls", "m3u8") or urlparse(url).path.lower().endswith(".m3u8")
