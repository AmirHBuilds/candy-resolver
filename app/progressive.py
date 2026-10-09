"""Progressive HLS download engine: ffprobe -> ffmpeg (-c copy) into a growing playlist, while reporting progress.
No database and no web framework in here: the caller passes `on_update`, so this is easy to test for real."""
import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from . import hls
from .runner import _kill, _read_capped
from .speedmeter import SpeedMeter

GB, MB = 1024 ** 3, 1024 ** 2


class ProgressiveError(Exception):
    """Always carries a short, human-readable reason (it is shown to the API client)."""


@dataclass
class Params:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    segment_seconds: int = 4
    min_segments: int = 3                 # playable once this many segments are finished
    max_bytes: int = 20 * GB
    reserve_bytes: int = 2 * GB
    timeout_s: int = 240 * 60
    probe_timeout_s: int = 60
    audio_bitrate: str = "128k"
    free_bytes: Callable[[], int] | None = None


@dataclass
class Update:
    size: int                  # bytes written so far (finished + in-progress segments)
    total_bytes: int | None    # projected final size (None until we know how far along we are)
    playable: bool
    segments: int              # finished segments listed in the playlist
    speed: int | None          # bytes/sec, smoothed over ~8s
    eta: int | None            # seconds
    out_seconds: float         # seconds of video written so far


def _limit_text(n: int) -> str:
    return f"{n / GB:.0f} GB" if n >= GB else f"{n / MB:.0f} MB"


def _short(err: bytes, n: int = 220) -> str:
    lines = [ln.strip() for ln in err.decode("utf-8", "replace").splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:n]


async def _probe(params: Params, url: str, headers: dict, ua: str, hls_source: bool) -> dict:
    cmd = [params.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json",
           *hls.input_opts(headers, ua, hls_source), url]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), params.probe_timeout_s)
    except asyncio.TimeoutError:
        raise ProgressiveError("could not read the source (timed out)")
    finally:
        if proc.returncode is None:
            _kill(proc)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:
                pass
    if proc.returncode != 0:
        raise ProgressiveError("could not read the source: " + (_short(err) or "unknown error"))
    try:
        return hls.parse_probe(json.loads(out))
    except ValueError:
        raise ProgressiveError("could not read the source (unreadable probe output)")


def _scan(dest_dir: Path) -> tuple[int, int]:
    """(bytes on disk, finished segments listed in the playlist)."""
    size = 0
    try:
        entries = list(os.scandir(dest_dir))
    except FileNotFoundError:
        return 0, 0
    for e in entries:
        n = e.name
        if hls.SEG_NAME.fullmatch(n) or (n.startswith("seg_") and n.endswith(".ts.tmp")):
            try:
                size += e.stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                pass          # renamed while we looked
    try:
        listed = hls.count_listed_segments((dest_dir / hls.PLAYLIST_NAME).read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        listed = 0
    return size, listed


async def run_progressive(*, video_url: str, audio_url: str | None = None, source_is_hls: bool = False,
                          headers: dict, user_agent: str, dest_dir, params: Params,
                          on_update: Callable[[Update], Awaitable[None]],
                          known_duration: float | None = None) -> dict:
    """Returns {"size", "segments", "duration"} when the whole thing is downloaded and the playlist is complete.
    On ANY failure or cancellation: ffmpeg is killed, dest_dir is deleted, and ProgressiveError (or the
    cancellation) propagates. A playlist that never ends is never left behind."""
    dest_dir = Path(dest_dir)
    proc = None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        vinfo = await _probe(params, video_url, headers, user_agent, source_is_hls)
        ainfo = await _probe(params, audio_url, headers, user_agent, source_is_hls) if audio_url else None
        try:
            hls.validate_probe(vinfo)
        except ValueError as e:
            raise ProgressiveError(str(e))
        duration = known_duration or vinfo["duration"]
        if vinfo["size"] and vinfo["size"] > params.max_bytes:
            raise ProgressiveError(f"file is larger than the {_limit_text(params.max_bytes)} limit")
        if vinfo["size"] and params.free_bytes and params.free_bytes() - vinfo["size"] < params.reserve_bytes:
            raise ProgressiveError("not enough free disk space")

        cmd = hls.build_progressive_cmd(params.ffmpeg, video_url, audio_url, dest_dir, headers, user_agent,
                                        vinfo, ainfo, source_is_hls, params.segment_seconds, params.audio_bitrate)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        except FileNotFoundError:
            raise ProgressiveError("ffmpeg is not installed on the server")

        meter = SpeedMeter()
        dur_us = duration * 1e6 if duration else None
        st = {"out_us": 0, "playable": False}

        async def progress():
            async for raw in proc.stdout:
                k, _, v = raw.decode(errors="replace").strip().partition("=")
                if k in ("out_time_us", "out_time_ms") and v.lstrip("-").isdigit():
                    st["out_us"] = max(st["out_us"], int(v))      # never goes backwards

        async def monitor():
            last_emit = 0.0
            while True:
                done = proc.returncode is not None
                size, listed = _scan(dest_dir)
                now = time.monotonic()
                if size > params.max_bytes:
                    raise ProgressiveError(f"file exceeds the {_limit_text(params.max_bytes)} limit")
                if params.free_bytes and params.free_bytes() < params.reserve_bytes:
                    raise ProgressiveError("ran out of free disk space")
                meter.add(size, now)
                flipped = listed >= params.min_segments and not st["playable"]
                if flipped:
                    st["playable"] = True
                if flipped or done or now - last_emit >= 2.0:
                    last_emit = now
                    total = max(size, int(size * dur_us / st["out_us"])) if dur_us and st["out_us"] > 0 else None
                    await on_update(Update(size, total, st["playable"], listed, meter.speed(),
                                           meter.eta(total - size if total else None), st["out_us"] / 1e6))
                if done:
                    return
                await asyncio.sleep(0.5)

        async def collect():
            _, err, _, _ = await asyncio.gather(
                progress(), _read_capped(proc.stderr, 16 * 1024, False), monitor(), proc.wait())
            return err

        try:
            err = await asyncio.wait_for(collect(), params.timeout_s)
        except asyncio.TimeoutError:
            raise ProgressiveError("download timed out after " + (f"{params.timeout_s // 60} minutes" if params.timeout_s >= 120 else f"{params.timeout_s} seconds"))
        finally:
            if proc.returncode is None:
                _kill(proc)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except Exception:
                    pass

        if proc.returncode != 0:
            raise ProgressiveError(_short(err) or f"ffmpeg exited with code {proc.returncode}")
        try:
            text = (dest_dir / hls.PLAYLIST_NAME).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise ProgressiveError("download finished but no playlist was written")
        if not hls.has_endlist(text):
            raise ProgressiveError("download ended but the playlist is incomplete")
        # A source cut off early makes ffmpeg exit cleanly with less video than expected: don't call that "ready".
        if dur_us and st["out_us"] < dur_us - max(10e6, 0.05 * dur_us):
            raise ProgressiveError(f"download incomplete (got {st['out_us'] / 1e6:.0f}s of {duration:.0f}s)")
        size, listed = _scan(dest_dir)
        return {"size": size, "segments": listed, "duration": duration}
    except BaseException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
