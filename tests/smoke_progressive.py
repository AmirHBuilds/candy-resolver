"""Progressive HLS: real ffmpeg + a local HTTP server with Range support (throttled, so the download takes a while).
Checks playable-before-ready, signed playlist/segments, path traversal, CORS, failures (no leftovers), cancellation.
Run: python -m tests.smoke_progressive"""
import asyncio, http.server, json, os, re, shutil, subprocess, tempfile, threading, time
from pathlib import Path
from types import SimpleNamespace

from app import hls, signing, speedmeter
from app.config import settings
from app.hlsserve import cors_headers, handle_hls_request, precheck, preflight
from app.progressive import Params, ProgressiveError, run_progressive

settings.signing_secret = "test-secret"
ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


# ---------------------------------------------------------------- speed meter (pure)
m = speedmeter.SpeedMeter(window=8)
for t in range(21):
    m.add(t * 1_000_000, now=float(t))
check("speed: steady 1 MB/s", abs(m.speed() - 1_000_000) < 1000, str(m.speed()))
m = speedmeter.SpeedMeter(window=8)
for t in range(21):
    m.add(t * 1_000_000 if t <= 10 else 10_000_000 + (t - 10) * 2_000_000, now=float(t))
check("speed: follows a change within ~8s (smoothed, not instant)", 1_850_000 < m.speed() < 2_050_000, str(m.speed()))
check("eta = remaining / speed", m.eta(10_000_000) == 5, str(m.eta(10_000_000)))
check("no data -> None", speedmeter.SpeedMeter().speed() is None and speedmeter.SpeedMeter().eta(5) is None)
m = speedmeter.SpeedMeter(); m.add(100, now=0); m.add(100, now=3)
check("stalled download -> speed 0, eta None", m.speed() == 0 and m.eta(1000) is None)
speedmeter.publish("x", 5, 6); check("registry read/clear", speedmeter.read("x") == (5, 6) and (speedmeter.clear("x") or speedmeter.read("x") == (None, None)))

# ---------------------------------------------------------------- playlist rewrite + names (pure)
pl = "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:4.0,\nseg_00000.ts\n#EXTINF:4.0,\nseg_00001.ts\n#EXTINF:4.0,\nseg_0000"
out = hls.rewrite_playlist(pl, "exp=1&sig=ab", lambda n: n == "seg_00000.ts")
check("rewrite: adds query to finished segments, drops missing/partial ones", "seg_00000.ts?exp=1&sig=ab" in out and "seg_00001" not in out and "seg_0000\n" not in out)
full = pl.rsplit("#EXTINF", 1)[0] + "#EXT-X-ENDLIST\n"
check("rewrite: ENDLIST dropped if a segment is missing", "ENDLIST" not in hls.rewrite_playlist(full, "q", lambda n: n == "seg_00000.ts"))
check("rewrite: ENDLIST kept when everything exists", "ENDLIST" in hls.rewrite_playlist(full, "q", lambda n: True))
check("filenames: strict", all(hls.valid_hls_filename(n) for n in ("index.m3u8", "seg_00012.ts")) and not any(
    hls.valid_hls_filename(n) for n in ("../index.m3u8", "seg_1.ts", "seg_00001.ts.tmp", "seg_00001.ts/../x", "index.m3u8/", "", "INDEX.m3u8", "seg_00001.TS", "..%2findex.m3u8")))
check("cors: list / star / none", cors_headers("https://a.com", "https://a.com,https://b.com")["Access-Control-Allow-Origin"] == "https://a.com"
      and cors_headers("https://evil.com", "https://a.com").get("Access-Control-Allow-Origin") is None
      and cors_headers("https://x.com", "*")["Access-Control-Allow-Origin"] == "*" and cors_headers("https://x.com", "") == {})
c = cors_headers("https://a.com", "https://a.com")
check("cors: Range allowed, Content-Range/Length/Accept-Ranges exposed", c["Access-Control-Allow-Headers"] == "Range" and all(
    h in c["Access-Control-Expose-Headers"] for h in ("Content-Length", "Content-Range", "Accept-Ranges")) and "GET" in c["Access-Control-Allow-Methods"] and "OPTIONS" in c["Access-Control-Allow-Methods"])
check("cors: preflight is 204 with headers", preflight("https://a.com", "https://a.com").status == 204)

# ---------------------------------------------------------------- sample media + a Range-capable, throttled HTTP server
ROOT = Path(tempfile.mkdtemp(prefix="cr_prog_"))


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


V = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25"]
A = ["-f", "lavfi", "-i", "sine=frequency=440"]
H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", "600k", "-maxrate", "600k", "-bufsize", "1200k", "-g", "25",
        "-keyint_min", "25", "-sc_threshold", "0", "-pix_fmt", "yuv420p"]
print("... generating samples")
ff(*V, *A, "-t", "40", *H264, "-c:a", "aac", "-movflags", "+faststart", str(ROOT / "good.mp4"))
ff(*V, *A, "-t", "8", *H264, "-c:a", "ac3", "-movflags", "+faststart", str(ROOT / "ac3.mp4"))
ff(*V, *A, "-t", "6", "-c:v", "mpeg4", "-c:a", "aac", str(ROOT / "mpeg4.mp4"))
ff(*V, *A, "-t", "4", "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(ROOT / "hevc.mp4"))
ff(*V, *A, "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p", "-c:a", "aac", str(ROOT / "yuv444.mp4"))
(ROOT / "garbage.mp4").write_bytes(os.urandom(200_000))
shutil.copy(ROOT / "good.mp4", ROOT / "cut.mp4")
(ROOT / "src").mkdir()
ff("-i", str(ROOT / "good.mp4"), "-c", "copy", "-f", "hls", "-hls_time", "4", "-hls_list_size", "0", "-hls_playlist_type", "vod",
   "-hls_segment_filename", str(ROOT / "src" / "s%d.ts"), str(ROOT / "src" / "index.m3u8"))
CUT_FRACTION = 0.55


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log_message = lambda *a, **k: None

    def _serve(self, head):
        f = ROOT / self.path.split("?")[0].lstrip("/")
        if not f.is_file():
            self.send_error(404); return
        size = f.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            mm = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(mm.group(1)); end = int(mm.group(2)) if mm.group(2) else size - 1; status = 206
        cut = int(size * CUT_FRACTION) if f.name == "cut.mp4" else None
        if start >= size or (cut is not None and start >= cut):
            self.send_error(416 if start >= size else 404); return
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4" if f.suffix == ".mp4" else "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        sent, limit = 0, (cut - start if cut is not None else end - start + 1)
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                while sent < limit:
                    chunk = fh.read(min(65536, limit - sent)); self.wfile.write(chunk); sent += len(chunk)
                    if f.name in ("good.mp4", "cut.mp4"):
                        time.sleep(0.12)                      # ~500 KB/s: the download takes several seconds
        except (BrokenPipeError, ConnectionResetError):
            pass
        if cut is not None:
            self.close_connection = True                      # server dies midway

    def do_GET(self): self._serve(False)
    def do_HEAD(self): self._serve(True)


class Quiet(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):      # the "dying" test server resets connections on purpose
        pass


srv = Quiet(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
LIB = Path(tempfile.mkdtemp(prefix="cr_lib_"))
ID = "lib_0123456789abcdef"


def params(**kw):
    return Params(min_segments=3, segment_seconds=4, timeout_s=60, free_bytes=lambda: 10 ** 12, reserve_bytes=0, **kw)


def item(status="downloading", playable=True, mode="hls", delete_at=None):
    return SimpleNamespace(id=ID, mode=mode, status=status, playable=playable, delete_at=delete_at)


def serve(filename, *, it=None, exp=None, sig=None, **kw):
    e, s = signing.sign(ID, 600)
    return handle_hls_request(item=it if it is not None else item(), item_id=ID, base_dir=LIB, filename=filename,
                              exp=str(exp if exp is not None else e), sig=sig if sig is not None else s, **kw)


async def main():
    import datetime as dt
    cmd = hls.build_progressive_cmd("ffmpeg", "http://x/a.mp4", None, "/o", {}, "ua",
                                    {"video_index": 0, "audio_index": 1, "audio_codec": "aac"}, None, False)
    check("never re-encodes video, never uses -re", "-re" not in cmd and cmd[cmd.index("-c:v") + 1] == "copy" and "libx264" not in cmd)
    cmd = hls.build_progressive_cmd("ffmpeg", "http://x/a.mp4", None, "/o", {}, "ua",
                                    {"video_index": 0, "audio_index": 1, "audio_codec": "ac3"}, None, False)
    check("non-aac/mp3 audio is converted to aac; subtitles/data dropped", cmd[cmd.index("-c:a") + 1] == "aac" and "-sn" in cmd and "-dn" in cmd)
    check("plain http gets reconnect options", "-reconnect" in cmd and "-reconnect_streamed" in cmd)

    # ============ A) the real download, observed from the outside ============
    dest = LIB / ID
    updates, early = [], {}

    async def on_update(u):
        updates.append((time.monotonic(), u))
        if u.playable and not early:
            r = serve("index.m3u8")
            body = r.body.decode()
            early.update(playlist=r, body=body, t=time.monotonic())
            e, s = signing.sign(ID, 600)
            early["seg"] = serve("seg_00000.ts", range_header="bytes=0-99")
            early["head"] = serve("seg_00000.ts", method="HEAD")
            early["bad_sig"] = serve("index.m3u8", sig="0" * 64)
            early["bad_exp"] = serve("index.m3u8", exp=int(e) + 5)
            early["old"] = serve("index.m3u8", exp=int(time.time()) - 5, sig=signing._mac(ID, int(time.time()) - 5))
            early["unicode"] = precheck(ID, "index.m3u8", str(e), "é" * 64)
            early["traversal"] = [serve(n, exp=e, sig=s).status for n in
                                  ("../index.m3u8", "..%2findex.m3u8", "seg_00000.ts/../../x", "seg_00000.ts.tmp", "seg_1.ts", "../../etc/passwd", "index.m3u8/")]
            early["not_playable"] = serve("index.m3u8", it=item(playable=False)).status
            early["as_file_item"] = serve("index.m3u8", it=item(mode="file")).status
            early["failed_item"] = serve("index.m3u8", it=item(status="failed")).status
            early["no_item"] = serve("index.m3u8", it=False or None).status if False else handle_hls_request(
                item=None, item_id=ID, base_dir=LIB, filename="index.m3u8", exp=str(e), sig=s).status
            early["wrong_id_sig"] = handle_hls_request(item=item(), item_id="lib_ffffffffffffffff", base_dir=LIB, filename="index.m3u8", exp=str(e), sig=s).status
            early["cors"] = serve("index.m3u8", origin="https://candyflix.example", allowed_origins="https://candyflix.example")

    t0 = time.monotonic()
    res = await run_progressive(video_url=f"{BASE}/good.mp4", headers={}, user_agent="candyresolver-test", dest_dir=dest,
                                params=params(), on_update=on_update)
    t_done = time.monotonic()
    dur = t_done - t0
    check("download completed", res["segments"] >= 9 and res["size"] > 0, f"({dur:.1f}s, {res['segments']} segments, {res['size']} bytes)")
    check("became playable BEFORE the download finished", early and early["t"] < t_done - 1.5, f"(playable at +{early['t'] - t0:.1f}s of {dur:.1f}s)")
    r = early["playlist"]
    check("playable playlist: 200, no ENDLIST yet, >=3 segments, EVENT type", r.status == 200 and "ENDLIST" not in early["body"]
          and early["body"].count("seg_") >= 3 and "PLAYLIST-TYPE:EVENT" in early["body"])
    check("growing playlist is Cache-Control: no-store", r.headers["Cache-Control"] == "no-store" and r.headers["Content-Type"] == "application/vnd.apple.mpegurl")
    segs = re.findall(r"^(seg_\d{5}\.ts)\?exp=(\d+)&sig=([0-9a-f]{64})$", early["body"], re.M)
    check("every segment URI carries exp + sig", len(segs) == early["body"].count("seg_") and len(segs) >= 3)
    sv = serve(segs[0][0], exp=segs[0][1], sig=segs[0][2])
    check("rewritten segment URL is accepted", sv.status == 200 and sv.headers["Content-Type"] == "video/mp2t" and sv.file[2] > 0)
    check("segment Range -> 206 + Content-Range", early["seg"].status == 206 and early["seg"].headers["Content-Length"] == "100" and early["seg"].headers["Content-Range"].startswith("bytes 0-99/"))
    check("segment HEAD has headers, no body", early["head"].status == 200 and early["head"].file is None and int(early["head"].headers["Content-Length"]) > 0)
    check("bad signature -> 403", early["bad_sig"].status == 403)
    check("tampered expiry -> 403", early["bad_exp"].status == 403)
    check("expired link -> 403", early["old"].status == 403)
    check("non-ASCII signature -> 403 (no crash)", early["unicode"] is not None and early["unicode"].status == 403)
    check("path traversal / odd names all rejected (404)", set(early["traversal"]) == {404}, str(early["traversal"]))
    check("not playable yet -> 404", early["not_playable"] == 404)
    check("an HLS item is not served as a file item, and vice versa -> 404", early["as_file_item"] == 404)
    check("failed item -> 404 (a dead playlist never lingers)", early["failed_item"] == 404)
    check("unknown item / signature for another item -> 404/403", early["no_item"] == 404 and early["wrong_id_sig"] == 403)
    check("CORS headers on the playlist", early["cors"].headers.get("Access-Control-Allow-Origin") == "https://candyflix.example")

    final = (dest / "index.m3u8").read_text()
    check("final playlist has ENDLIST", "#EXT-X-ENDLIST" in final)
    done_item = item(status="ready", delete_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    r = serve("index.m3u8", it=done_item)
    check("ready: whole playlist served, may be cached briefly", r.status == 200 and "ENDLIST" in r.body.decode() and r.headers["Cache-Control"] == "private, max-age=60")
    check("same signed URL keeps working after ready", serve("seg_00000.ts", it=done_item).status == 200)
    check("past its delete time -> 404", serve("index.m3u8", it=item(status="ready", delete_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))).status == 404)

    ratios = [round(u.size / u.total_bytes, 4) for _, u in updates if u.total_bytes]
    check("progress never goes backwards", ratios == sorted(ratios) and ratios and max(ratios) <= 1.0, f"({len(ratios)} samples, last {ratios[-1] if ratios else None})")
    check("size never goes backwards", [u.size for _, u in updates] == sorted(u.size for _, u in updates))
    check("speed reported while downloading", any(u.speed and u.speed > 50_000 for _, u in updates), str([u.speed for _, u in updates if u.speed][:4]))
    check("eta reported while downloading", any(u.eta is not None for _, u in updates))
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,crypto", "-show_streams", "-show_format", "-of", "json",
                                       str(dest / "index.m3u8")], capture_output=True, text=True).stdout)
    codecs = {s["codec_type"]: s["codec_name"] for s in probe["streams"]}
    check("output is h264 + aac, ~40s", codecs == {"video": "h264", "audio": "aac"} and 38 < float(probe["format"]["duration"]) < 42, str(codecs))
    dec = subprocess.run(["ffmpeg", "-v", "error", "-protocol_whitelist", "file,crypto", "-i", str(dest / "index.m3u8"), "-f", "null", "-"], capture_output=True, text=True)
    check("whole output decodes without errors", dec.returncode == 0 and not dec.stderr.strip(), dec.stderr[:100])
    check("no temp files left behind", not [p for p in dest.iterdir() if p.name.endswith(".tmp")])
    shutil.rmtree(dest)

    # ============ B) audio conversion, HLS source ============
    d2 = LIB / "lib_aaaaaaaaaaaaaaaa"
    await run_progressive(video_url=f"{BASE}/ac3.mp4", headers={}, user_agent="t", dest_dir=d2, params=params(), on_update=lambda u: asyncio.sleep(0))
    pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,crypto", "-show_streams", "-of", "json", str(d2 / "index.m3u8")], capture_output=True, text=True).stdout)
    check("ac3 audio converted to aac, video still h264 (copied)", {s["codec_type"]: s["codec_name"] for s in pr["streams"]} == {"video": "h264", "audio": "aac"})
    shutil.rmtree(d2)
    d3 = LIB / "lib_bbbbbbbbbbbbbbbb"
    r3 = await run_progressive(video_url=f"{BASE}/src/index.m3u8", source_is_hls=True, known_duration=40.0, headers={}, user_agent="t", dest_dir=d3,
                               params=params(), on_update=lambda u: asyncio.sleep(0))
    check("an HLS source works too", r3["segments"] >= 9 and "ENDLIST" in (d3 / "index.m3u8").read_text(), str(r3))
    shutil.rmtree(d3)

    # ============ C) failures: clear error, nothing left behind ============
    async def fails(name, url, needle, dest_id, **kw):
        d = LIB / dest_id
        try:
            await run_progressive(video_url=url, headers={}, user_agent="t", dest_dir=d, params=kw.pop("p", params()), on_update=lambda u: asyncio.sleep(0), **kw)
            check(name, False, "- no error raised")
        except ProgressiveError as e:
            check(name, needle.lower() in str(e).lower() and not d.exists(), f"-> “{e}”" + ("" if not d.exists() else " (LEFTOVER FOLDER)"))

    await fails("hevc source -> clear error", f"{BASE}/hevc.mp4", "unsupported video codec: hevc", "lib_cccccccccccccc01")
    await fails("mpeg4 source -> clear error", f"{BASE}/mpeg4.mp4", "unsupported video codec: mpeg4", "lib_cccccccccccccc02")
    await fails("10-bit/4:4:4 h264 -> clear error", f"{BASE}/yuv444.mp4", "unsupported video format", "lib_cccccccccccccc03")
    await fails("garbage file -> failed", f"{BASE}/garbage.mp4", "could not read the source", "lib_cccccccccccccc04")
    await fails("404 source -> failed", f"{BASE}/nope.mp4", "could not read the source", "lib_cccccccccccccc05")
    await fails("size limit", f"{BASE}/good.mp4", "larger than", "lib_cccccccccccccc06", p=Params(max_bytes=1000, free_bytes=lambda: 10 ** 12, reserve_bytes=0))
    await fails("free-disk reserve", f"{BASE}/good.mp4", "free disk", "lib_cccccccccccccc07", p=Params(free_bytes=lambda: 5, reserve_bytes=10 ** 9))
    t1 = time.monotonic()
    await fails("source that dies midway -> failed, not 'ready'", f"{BASE}/cut.mp4", "", "lib_cccccccccccccc08", p=params())
    print(f"     (cut source took {time.monotonic() - t1:.1f}s to fail)")
    await fails("overall timeout is enforced", f"{BASE}/good.mp4", "timed out", "lib_cccccccccccccc09", p=Params(timeout_s=2, free_bytes=lambda: 10 ** 12, reserve_bytes=0))

    # ============ D) cancellation kills ffmpeg and cleans up ============
    d4 = LIB / "lib_dddddddddddddddd"
    flag = asyncio.Event()

    async def seen(u):
        if u.playable: flag.set()
    task = asyncio.create_task(run_progressive(video_url=f"{BASE}/good.mp4", headers={}, user_agent="t", dest_dir=d4, params=params(), on_update=seen))
    await asyncio.wait_for(flag.wait(), 30)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.5)
    alive = subprocess.run(["pgrep", "-f", str(d4)], capture_output=True).returncode == 0
    check("cancel: ffmpeg is gone and the folder is deleted", task.cancelled() and not alive and not d4.exists())

    srv.shutdown()
    shutil.rmtree(ROOT, ignore_errors=True); shutil.rmtree(LIB, ignore_errors=True)
    print("\nALL PASSED" if ok else "\nSOME FAILED")


asyncio.run(main())
