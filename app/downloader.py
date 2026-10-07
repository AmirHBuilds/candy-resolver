"""Downloads a stream into the library (our own disk) in the background.
Plain files are streamed with httpx; HLS (m3u8) playlists are remuxed to mp4 with ffmpeg."""
import asyncio
import os
import shutil
import time
from datetime import timedelta

import httpx

from . import hls
from .config import settings
from .db import SessionLocal
from .fileutil import is_hls, pick_ext
from .models import LibraryItem, utcnow
from .runner import _kill, _read_capped

GB = 1024 ** 3
MB = 1024 ** 2

downloads: dict[str, asyncio.Task] = {}
_sem: asyncio.Semaphore | None = None


class DownloadError(Exception):
    pass


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(settings.library_concurrency)
    return _sem


def start_download(item_id: str, url: str, headers: dict | None, fmt: str | None) -> None:
    t = asyncio.create_task(_download(item_id, url, headers, fmt))
    downloads[item_id] = t
    t.add_done_callback(lambda _: downloads.pop(item_id, None))


def cancel_download(item_id: str) -> bool:
    t = downloads.get(item_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


def _free_bytes() -> int:
    return shutil.disk_usage(settings.library_path).free


def _limits():
    return settings.library_max_size_gb * GB, settings.library_min_free_gb * GB


async def _fetch_direct(db, item, url: str, hdrs: dict, part) -> int:
    max_bytes, reserve = _limits()
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0)) as client:
        async with client.stream("GET", url, headers=hdrs) as r:
            r.raise_for_status()
            if "mpegurl" in r.headers.get("content-type", "").lower():
                raise DownloadError("this URL is an HLS playlist - the source script should return format 'hls'")
            cl = r.headers.get("content-length", "")
            total = int(cl) if cl.isdigit() else None
            if total and total > max_bytes:
                raise DownloadError(f"file is larger than the {settings.library_max_size_gb} GB limit")
            if total and _free_bytes() - total < reserve:
                raise DownloadError("not enough free disk space")
            item.total_bytes = total
            await db.commit()

            written = last_disk_check = 0
            last_commit = time.monotonic()
            with open(part, "wb") as f:
                async for chunk in r.aiter_bytes(MB):
                    written += len(chunk)
                    if written > max_bytes:
                        raise DownloadError("file exceeds the size limit")
                    if written - last_disk_check >= 64 * MB:
                        last_disk_check = written
                        if _free_bytes() < reserve:
                            raise DownloadError("ran out of free disk space")
                    await asyncio.to_thread(f.write, chunk)
                    if time.monotonic() - last_commit >= 2:
                        item.size = written
                        await db.commit()
                        last_commit = time.monotonic()
    if written == 0:
        raise DownloadError("empty download")
    if total and written != total:
        raise DownloadError(f"download incomplete ({written} of {total} bytes)")
    return written


async def _get_text(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    r = await client.get(url)
    r.raise_for_status()
    if len(r.content) > 5 * MB:
        raise DownloadError("playlist is unreasonably large")
    return r.text, str(r.url)


async def _fetch_hls(db, item, url: str, hdrs: dict, part) -> int:
    max_bytes, reserve = _limits()
    hdrs = dict(hdrs)
    ua = hdrs.pop("User-Agent", "candyresolver/0.1")
    hdrs.pop("Accept-Encoding", None)

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0, headers={"User-Agent": ua, **hdrs}) as c:
        text, video_url = await _get_text(c, url)
        audio_url = None
        if hls.is_master(text):
            variants, audio = hls.parse_master(text, video_url)
            v = hls.pick_variant(variants, item.quality)
            if v is None:
                raise DownloadError("playlist has no playable variants")
            a = hls.pick_audio(audio, v["audio"])
            audio_url = a["url"] if a else None
            text, video_url = await _get_text(c, v["url"])
            if item.quality is None and v["height"]:
                item.quality = f"{v['height']}p"
    info = hls.media_info(text)
    if not info["ended"] and not info["vod"]:
        raise DownloadError("live or unfinished HLS streams are not supported")
    if info["duration"] <= 0:
        raise DownloadError("playlist has no segments")
    await db.commit()

    cmd = hls.build_ffmpeg_cmd(settings.ffmpeg_path, video_url, audio_url, part, hdrs, ua)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        raise DownloadError("ffmpeg is not installed on the server")

    duration_us = info["duration"] * 1e6

    async def progress():
        out_us = size = 0
        last_commit = time.monotonic()
        async for raw in proc.stdout:
            key, _, val = raw.decode(errors="replace").strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and val.lstrip("-").isdigit():
                out_us = int(val)
            elif key == "total_size" and val.isdigit():
                size = int(val)
            elif key == "progress" and time.monotonic() - last_commit >= 2:
                last_commit = time.monotonic()
                if size > max_bytes:
                    raise DownloadError("file exceeds the size limit")
                if _free_bytes() < reserve:
                    raise DownloadError("ran out of free disk space")
                item.size = size
                if out_us > 0:  # project the final size from how far along we are
                    item.total_bytes = max(size, int(size * duration_us / out_us))
                await db.commit()

    try:
        _, err, _ = await asyncio.wait_for(
            asyncio.gather(progress(), _read_capped(proc.stderr, 16 * 1024, False), proc.wait()),
            settings.library_hls_timeout_min * 60)
    except asyncio.TimeoutError:
        raise DownloadError(f"HLS download timed out after {settings.library_hls_timeout_min} minutes")
    finally:
        if proc.returncode is None:
            _kill(proc)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:
                pass
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip()[-600:]
        raise DownloadError(msg or f"ffmpeg exited with code {proc.returncode}")
    written = os.path.getsize(part) if os.path.exists(part) else 0
    if written == 0:
        raise DownloadError("empty download")
    return written


async def _download(item_id: str, url: str, headers: dict | None, fmt: str | None) -> None:
    async with _semaphore():
        async with SessionLocal() as db:
            item = await db.get(LibraryItem, item_id)
            if item is None or item.status != "queued":
                return  # deleted before it started
            item.status = "downloading"
            await db.commit()

            hls_mode = is_hls(fmt, url)
            dest_dir = settings.library_path / item_id
            part = dest_dir / "video.part"
            final = dest_dir / ("video.mp4" if hls_mode else f"video.{pick_ext(fmt, url)}")
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                hdrs = {"User-Agent": "candyresolver/0.1", "Accept-Encoding": "identity"}
                hdrs.update({str(k): str(v) for k, v in (headers or {}).items()})

                written = await (_fetch_hls if hls_mode else _fetch_direct)(db, item, url, hdrs, part)

                os.replace(part, final)
                now = utcnow()
                item.status = "ready"
                item.file_path = str(final)
                item.format = final.suffix.lstrip(".")
                item.size = item.total_bytes = written
                item.ready_at = now
                item.delete_at = now + timedelta(hours=item.ttl_hours)
                await db.commit()
            except asyncio.CancelledError:
                shutil.rmtree(dest_dir, ignore_errors=True)  # the DELETE endpoint updates the row
                raise
            except Exception as e:
                shutil.rmtree(dest_dir, ignore_errors=True)
                await db.rollback()
                item = await db.get(LibraryItem, item_id)
                if item is not None:
                    item.status = "failed"
                    item.error = (str(e) or e.__class__.__name__)[:500]
                    item.file_path = None
                    await db.commit()
