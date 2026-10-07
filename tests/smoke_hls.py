"""HLS checks. Pure parsing always; a real ffmpeg end-to-end run if ffmpeg is installed.
Run: python -m tests.smoke_hls"""
import functools
import http.server
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.request import urlopen

from app import hls

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name)


MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=320x180,CODECS="avc1.4d401e,mp4a.40.2",AUDIO="aud"
lo.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=640x360,AUDIO="aud"
hi.m3u8
"""
variants, audio = hls.parse_master(MASTER, "https://cdn.test/v/master.m3u8")
check("master detected", hls.is_master(MASTER))
check("two variants, urls resolved", [v["url"] for v in variants] ==
      ["https://cdn.test/v/lo.m3u8", "https://cdn.test/v/hi.m3u8"])
check("quoted codecs with comma parsed", variants[0]["height"] == 180 and variants[0]["bandwidth"] == 500000)
check("separate audio found", hls.pick_audio(audio, "aud")["url"] == "https://cdn.test/v/audio/en.m3u8")
check("best by default", hls.pick_variant(variants)["height"] == 360)
check("720p -> best that fits", hls.pick_variant(variants, "720p")["height"] == 360)
check("180p -> exact", hls.pick_variant(variants, "180p")["height"] == 180)
check("100p -> smallest available", hls.pick_variant(variants, "100p")["height"] == 180)
check("junk quality -> best", hls.pick_variant(variants, "best")["height"] == 360)
info = hls.media_info("#EXTM3U\n#EXTINF:4.0,\na.ts\n#EXTINF:2.5,\nb.ts\n#EXT-X-ENDLIST\n")
check("duration + ended", info["duration"] == 6.5 and info["ended"])
check("live playlist flagged", not hls.media_info("#EXTM3U\n#EXTINF:4,\na.ts\n")["ended"])
cmd = hls.build_ffmpeg_cmd("ffmpeg", "http://x/v.m3u8", None, "out.part", {"Referer": "http://r/"}, "ua")
check("whitelist has no file protocol", "http,https,tcp,tls,crypto" in cmd and "file" not in cmd[cmd.index("-protocol_whitelist") + 1])
check("headers passed", "Referer: http://r/\r\n" in cmd)

if not shutil.which("ffmpeg"):
    print("SKIP ffmpeg end-to-end (ffmpeg not installed)")
else:
    tmp = Path(tempfile.mkdtemp())

    def make(name, size):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                        "-hls_segment_filename", str(tmp / f"{name}_%d.ts"), str(tmp / f"{name}.m3u8")], check=True)

    make("lo", "320x180"); make("hi", "640x360")
    (tmp / "master.m3u8").write_text("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=320x180\nlo.m3u8\n"
                                     "#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=640x360\nhi.m3u8\n")
    (tmp / "evil.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nfile:///etc/hostname\n#EXT-X-ENDLIST\n")

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    mtext = urlopen(f"{base}/master.m3u8").read().decode()
    v, _ = hls.parse_master(mtext, f"{base}/master.m3u8")
    chosen = hls.pick_variant(v, "720p")
    mediatext = urlopen(chosen["url"]).read().decode()
    check("real playlist: ended, ~6s", hls.media_info(mediatext)["ended"] and 5.5 < hls.media_info(mediatext)["duration"] < 6.5)

    out = tmp / "video.part"
    r = subprocess.run(hls.build_ffmpeg_cmd("ffmpeg", chosen["url"], None, out, {}, "candyresolver/0.1"),
                       capture_output=True, text=True, timeout=60)
    check("ffmpeg remux succeeded", r.returncode == 0 and out.exists() and out.stat().st_size > 0)
    check("progress output parseable", "out_time_us=" in r.stdout and "progress=end" in r.stdout)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(out)],
                                      capture_output=True, text=True).stdout)
    kinds = {s["codec_type"] for s in probe["streams"]}
    h = next(s["height"] for s in probe["streams"] if s["codec_type"] == "video")
    check("mp4 has video+audio at chosen quality", kinds == {"video", "audio"} and h == 360)
    check("mp4 duration ~6s", 5.5 < float(probe["format"]["duration"]) < 6.6)

    out2 = tmp / "evil.part"
    r = subprocess.run(hls.build_ffmpeg_cmd("ffmpeg", f"{base}/evil.m3u8", None, out2, {}, "ua"),
                       capture_output=True, text=True, timeout=60)
    check("playlist pointing at file:// is refused", r.returncode != 0 and (not out2.exists() or out2.stat().st_size == 0))
    srv.shutdown()

print("\nALL PASSED" if ok else "\nSOME FAILED")
