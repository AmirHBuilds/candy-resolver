"""Downloads a stream into the library (our own disk) in the background. Three ways, chosen per item:
  mode "file"  + plain URL   -> streamed with httpx into one file
  mode "file"  + HLS source  -> remuxed to a single mp4 with ffmpeg (-c copy)
  mode "hls"   (progressive) -> ffmpeg (-c copy) into a growing playlist + segments, playable while it downloads
Video is never re-encoded anywhere."""
import asyncio
import logging
import re
import os
import shutil
import time
from datetime import timedelta

import httpx
from sqlalchemy import update
from urllib.parse import urljoin

from . import hls, speedmeter
from .config import settings
from .db import SessionLocal
from .fileutil import is_hls, pick_ext
from .itemfiles import remove_item_dir
from .linkexpiry import dead_link_reason
from .notify import notifier
from .models import LibraryItem, Stream, as_utc, utcnow
from .progressive import Params, ProgressiveError, Update, run_progressive
from .runner import _kill, _read_capped
from .speedmeter import SpeedMeter
from .subs_job import cancel_subtitles

GB = 1024 ** 3
MB = 1024 ** 2

log = logging.getLogger("candy.download")
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


_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _short_urls(text: str) -> str:
    """Shrink every URL to scheme://host/...<last 25 chars> so a long signed URL cannot bury the real reason."""
    def short(m):
        u = m.group(0)
        host = u.split("/")[2] if u.count("/") >= 2 else u
        return f"{u.split('//')[0]}//{host}/...{u[-25:]}" if len(u) > 80 else u
    return _URL_RE.sub(short, text)


def _limits():
    return settings.library_max_size_gb * GB, settings.library_min_free_gb * GB


def _split_headers(hdrs: dict) -> tuple[dict, str]:
    h = dict(hdrs)
    ua = h.pop("User-Agent", "candyresolver/0.1")
    h.pop("Accept-Encoding", None)
    return h, ua


async def _fetch_direct(db, item, url: str, hdrs: dict, part) -> int:
    max_bytes, reserve = _limits()
    meter = SpeedMeter()
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
                    meter.add(written)
                    if written > max_bytes:
                        raise DownloadError("file exceeds the size limit")
                    if written - last_disk_check >= 64 * MB:
                        last_disk_check = written
                        if _free_bytes() < reserve:
                            raise DownloadError("ran out of free disk space")
                    await asyncio.to_thread(f.write, chunk)
                    if time.monotonic() - last_commit >= 2:
                        item.size = written
                        speedmeter.publish(item.id, meter.speed(), meter.eta(total - written if total else None))
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


async def _resolve_hls(db, item, url: str, hdrs: dict, ua: str) -> tuple[str, str | None, float]:
    """HLS source -> (media playlist url, separate audio playlist url or None, duration in seconds).
    Picks the variant the client asked for (default: best). Refuses live / unfinished streams."""
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
    return video_url, audio_url, info["duration"]


async def _diagnose_hls(video_url: str, hdrs: dict, ua: str) -> str:
    """Called when ffmpeg fails on a segment: fetch the first segment ourselves and describe what we got."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0, headers={"User-Agent": ua, **hdrs}) as c:
            text, base = await _get_text(c, video_url)
            segs = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
            if not segs:
                return "diagnosis: playlist has no segments"
            seg = urljoin(base, segs[0])
            r = await c.get(seg, headers={"Range": "bytes=0-65535"})
            b = r.content
            ct = r.headers.get("content-type", "?")
            info = f"diagnosis: first segment -> HTTP {r.status_code}, content-type {ct}, {len(b)} bytes, starts {b[:12].hex()} ({b[:12]!r})"
            if r.status_code >= 400:
                return info + " - the server refuses the segment (missing/wrong Referer, Origin, Cookie or User-Agent header?)"
            if b[:1] == b"<" or b[:1] == b"{":
                return info + " - got a web page / JSON instead of video"
            # MPEG-TS packets are 188 bytes, each starting with 0x47: look for that pattern after some junk prefix
            if len(b) >= 188 * 3 and not (b[0] == 0x47 and b[188] == 0x47):
                for off in range(1, min(len(b) - 376, 4096)):
                    if b[off] == 0x47 and b[off + 188] == 0x47 and b[off + 376] == 0x47:
                        return info + f" - valid MPEG-TS starts at byte {off}: the segment has {off} junk bytes in front (disguised segments, e.g. fake image header). ffmpeg cannot read this directly; the bytes must be stripped"
            return info
    except Exception as e:  # diagnosis must never hide the original error
        return f"diagnosis failed: {e.__class__.__name__}: {_short_urls(str(e))[:200]}"


async def _fetch_hls(db, item, url: str, hdrs: dict, part) -> int:
    """HLS source -> one mp4 file (ffmpeg remux, no re-encoding)."""
    hdrs, ua = _split_headers(hdrs)
    video_url, audio_url, duration = await _resolve_hls(db, item, url, hdrs, ua)
    if duration and duration > 0:
        item.duration_s = duration
    with_hdrs, bare = (hdrs, ua), ({}, None)
    first_try, second_try = (with_hdrs, bare) if settings.hls_send_custom_headers else (bare, with_hdrs)
    try:
        return await _run_hls_ffmpeg(db, item, part, video_url, audio_url, duration, *first_try)
    except DownloadError as e:
        first = str(e)
        local = ("size limit", "free disk", "timed out", "not installed", "empty download")  # not a request problem
        if any(s in first for s in local) or second_try == first_try:
            raise
        # Some CDNs refuse requests that carry custom headers / a custom User-Agent, others need them: try the other way once.
        try:
            return await _run_hls_ffmpeg(db, item, part, video_url, audio_url, duration, *second_try)
        except DownloadError as e2:
            raise DownloadError(first + "\n-- retry (" + ("bare" if second_try == bare else "with headers")
                                + ") also failed: " + str(e2)[-250:])


async def _run_hls_ffmpeg(db, item, part, video_url: str, audio_url: str | None, duration: float,
                          hdrs: dict, ua: str | None) -> int:
    max_bytes, reserve = _limits()
    cmd = hls.build_ffmpeg_cmd(settings.ffmpeg_path, video_url, audio_url, part, hdrs, ua)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        raise DownloadError("ffmpeg is not installed on the server")

    duration_us = duration * 1e6
    meter = SpeedMeter()

    async def progress():
        out_us = size = 0
        last_commit = time.monotonic()
        async for raw in proc.stdout:
            key, _, val = raw.decode(errors="replace").strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and val.lstrip("-").isdigit():
                out_us = max(out_us, int(val))
            elif key == "total_size" and val.isdigit():
                size = int(val)
                meter.add(size)
            elif key == "progress" and time.monotonic() - last_commit >= 2:
                last_commit = time.monotonic()
                if size > max_bytes:
                    raise DownloadError("file exceeds the size limit")
                if _free_bytes() < reserve:
                    raise DownloadError("ran out of free disk space")
                item.size = size
                if out_us > 0:  # project the final size from how far along we are
                    item.total_bytes = max(size, int(size * duration_us / out_us))
                speedmeter.publish(item.id, meter.speed(),
                                   meter.eta(item.total_bytes - size if item.total_bytes else None))
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
        msg = _short_urls(err.decode("utf-8", "replace").strip())[-600:]
        if "segment" in msg.lower() or "Invalid data" in msg:
            msg = msg[-350:] + "\n" + await _diagnose_hls(video_url, hdrs, ua or "Lavf")
        raise DownloadError(msg or f"ffmpeg exited with code {proc.returncode}")
    written = os.path.getsize(part) if os.path.exists(part) else 0
    if written == 0:
        raise DownloadError("empty download")
    return written


async def _probe_duration(path) -> float | None:
    """Video length in seconds from a finished file (ffprobe). Never raises: a missing duration is not an error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 60)
        except asyncio.TimeoutError:
            _kill(proc)
            return None
        val = float(out.decode().strip().splitlines()[0])
        return val if val > 0 else None
    except Exception:
        return None


async def _fetch_progressive(db, item, url: str, hdrs: dict, dest_dir, source_is_hls: bool) -> int:
    """Any source -> HLS playlist + segments in dest_dir, playable while it downloads."""
    hdrs, ua = _split_headers(hdrs)
    video_url, audio_url, duration = url, None, None
    if source_is_hls:
        video_url, audio_url, duration = await _resolve_hls(db, item, url, hdrs, ua)
    max_bytes, reserve = _limits()
    params = Params(
        ffmpeg=settings.ffmpeg_path, ffprobe=settings.ffprobe_path,
        segment_seconds=settings.progressive_segment_seconds, min_segments=settings.progressive_min_segments,
        max_bytes=max_bytes, reserve_bytes=reserve, timeout_s=settings.library_hls_timeout_min * 60,
        audio_bitrate=settings.progressive_audio_bitrate, free_bytes=_free_bytes)

    async def on_update(u: Update) -> None:
        item.size = max(item.size or 0, u.size)
        if u.total_bytes:
            item.total_bytes = max(u.total_bytes, item.size)
        if u.playable:
            item.playable = True
        if u.duration and u.duration > 0:
            item.duration_s = u.duration
        speedmeter.publish(item.id, u.speed, u.eta)
        await db.commit()

    # HLS sources: bare ffmpeg first (like `ffmpeg -i playlist.m3u8` by hand), then once WITH the script's
    # headers/UA if the source could not be read (or the other way round with HLS_SEND_CUSTOM_HEADERS=true).
    attempts = [(hdrs, ua)]
    if source_is_hls:
        attempts = [(hdrs, ua), ({}, None)] if settings.hls_send_custom_headers else [({}, None), (hdrs, ua)]
        if not hdrs and not ua:
            attempts = attempts[:1]
    last_err = ""
    for i, (h, u) in enumerate(attempts):
        try:
            result = await run_progressive(video_url=video_url, audio_url=audio_url, source_is_hls=source_is_hls,
                                           headers=h, user_agent=u, dest_dir=dest_dir, params=params,
                                           on_update=on_update, known_duration=duration)
            break
        except ProgressiveError as e:
            last_err = str(e)
            if i + 1 < len(attempts) and last_err.startswith("could not read the source"):
                continue
            raise DownloadError(last_err if i == 0 else f"{first_err}\n-- retry also failed: {last_err}")
        finally:
            if i == 0:
                first_err = last_err
    return result["size"]


async def _download(item_id: str, url: str, headers: dict | None, fmt: str | None) -> None:
    async with _semaphore():
        try:
            async with SessionLocal() as db:
                item = await db.get(LibraryItem, item_id)
                if item is None or item.status != "queued":
                    return  # deleted before it started
                item.status = "downloading"
                await db.commit()
                log.info("download %s started: %s %s mode=%s source=%s", item_id, item.title or item.task_id,
                         item.quality or "", item.mode or "file", item.source_name)

                progressive = (item.mode or "file") == "hls"
                source_is_hls = is_hls(fmt, url)
                dest_dir = settings.library_path / item_id
                part = dest_dir / "video.part"
                final = dest_dir / ("video.mp4" if source_is_hls else f"video.{pick_ext(fmt, url)}")
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    hdrs = {"User-Agent": "candyresolver/0.1", "Accept-Encoding": "identity"}
                    hdrs.update({str(k): str(v) for k, v in (headers or {}).items()})

                    if progressive:
                        written = await _fetch_progressive(db, item, url, hdrs, dest_dir, source_is_hls)
                        item.file_path = str(dest_dir / hls.PLAYLIST_NAME)
                        item.format = "hls"
                        item.playable = True
                    else:
                        written = await (_fetch_hls if source_is_hls else _fetch_direct)(db, item, url, hdrs, part)
                        os.replace(part, final)
                        if not item.duration_s:
                            item.duration_s = await _probe_duration(final)
                        item.file_path = str(final)
                        item.format = final.suffix.lstrip(".")
                    now = utcnow()
                    item.status = "ready"
                    item.size = item.total_bytes = written
                    item.ready_at = now
                    item.delete_at = now + timedelta(hours=item.ttl_hours)
                    await db.commit()
                    log.info("download %s finished: %.1f MB in %.0f s", item_id, written / 1048576,
                             (now - as_utc(item.requested_at)).total_seconds() if item.requested_at else 0)
                except asyncio.CancelledError:
                    log.info("download %s cancelled", item_id)
                    cancel_subtitles(item_id)
                    remove_item_dir(item_id)  # the DELETE endpoint updates the row
                    raise
                except Exception as e:
                    log.error("download %s FAILED: %s", item_id, _short_urls(str(e) or e.__class__.__name__)[:600], extra={"tg_skip": True})
                    cancel_subtitles(item_id)
                    remove_item_dir(item_id)  # never leave a playlist that never ends; subs/ goes with it
                    await db.rollback()
                    item = await db.get(LibraryItem, item_id)
                    if item is not None:
                        item.status = "failed"
                        msg = _short_urls(str(e) or e.__class__.__name__)[:1000]
                        reason = dead_link_reason(str(e))
                        if reason and item.stream_id:      # the SOURCE refused the link: never offer this stream again
                            item.error = f"source_link_expired ({reason}): {msg}"[:1000]
                            await db.execute(update(Stream).where(Stream.id == item.stream_id).values(dead=True))
                            log.warning("download %s: stream %s marked dead (%s)", item_id, item.stream_id, reason, extra={"tg_skip": True})
                        else:
                            item.error = msg
                        notifier.emit("download_errors", "Download failed" + (": source link expired" if reason else ""),
                                      {"Title": item.title, "Source": item.source_name, "Quality": item.quality, "Mode": item.mode,
                                       "Item": item_id, "Reason": reason},
                                      msg, key=f"dl:{item.source_name}:{item.quality}:{re.sub(r'https?://[^ ]+|[0-9]+', '#', msg)[:80]}")
                        item.file_path = None
                        item.playable = False
                        await db.commit()
        finally:
            speedmeter.clear(item_id)
