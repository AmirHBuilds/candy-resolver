"""Audio extraction with a REAL ffmpeg: an mp4, an HLS folder (like progressive downloads) and a file without audio.
Run: python -m tests.smoke_audio"""
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from app.audio import AudioError, AudioStream, build_cmd, probe_audio

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    return [s["codec_type"] for s in d["streams"]], [s["codec_name"] for s in d["streams"]], float(d["format"].get("duration") or 0)


async def extract(path, fmt, track=0, start=None, duration=None):
    codecs, total = await probe_audio("ffprobe", path)
    if not codecs:
        raise AudioError("no audio", 422)
    if start and total and start >= total:
        raise AudioError("start is after the end", 422)
    s = AudioStream(build_cmd("ffmpeg", path, fmt, track, codecs[track], start, duration))
    await s.start()
    return b"".join([c async for c in s.chunks()])


async def main():
    tmp = Path(tempfile.mkdtemp())
    mp4, noaudio = tmp / "video.mp4", tmp / "silent.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=6", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(mp4))
    ff("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(noaudio))
    hls = tmp / "hls"; hls.mkdir()
    ff("-i", str(mp4), "-c", "copy", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
       "-hls_segment_filename", str(hls / "seg_%05d.ts"), str(hls / "index.m3u8"))

    for fmt, codec in (("aac", "aac"), ("mp3", "mp3"), ("opus", "opus")):
        data = await extract(mp4, fmt)
        f = tmp / f"out.{fmt}"; f.write_bytes(data)
        types, codecs, dur = probe(f)
        check(f"mp4 -> {fmt}: audio only, right codec, about 6 s", types == ["audio"] and codecs == [codec] and 5 < dur < 7, f"{types} {codecs} {dur:.1f}s")

    data = await extract(hls / "index.m3u8", "aac")
    f = tmp / "hls.aac"; f.write_bytes(data)
    types, _, dur = probe(f)
    check("HLS folder (playlist + segments) -> audio only", types == ["audio"] and 5 < dur < 7, f"{dur:.1f}s")

    data = await extract(mp4, "mp3", start=2, duration=2)
    f = tmp / "cut.mp3"; f.write_bytes(data)
    _, _, dur = probe(f)
    check("start=2 duration=2 gives about 2 s", 1.5 < dur < 2.6, f"{dur:.1f}s")

    try:
        await extract(mp4, "mp3", start=50)
        check("start beyond the end is refused", False)
    except AudioError as e:
        check("start beyond the end is refused", e.status == 422, str(e))

    try:
        await extract(noaudio, "aac")
        check("file without audio is refused", False)
    except AudioError as e:
        check("file without audio is refused", e.status == 422, str(e))

    try:
        await probe_audio("ffprobe", tmp / "missing.mp4")
        check("missing file is an error", False)
    except AudioError as e:
        check("missing file is an error", True, str(e))

    # client leaves early: the ffmpeg process must be gone
    s = AudioStream(build_cmd("ffmpeg", mp4, "mp3", 0, "aac"))
    await s.start()
    gen = s.chunks()
    await gen.__anext__()
    await gen.aclose()
    await asyncio.sleep(0.2)
    check("ffmpeg is stopped when the client disconnects", s.proc.returncode is not None)

    print("ALL OK" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
