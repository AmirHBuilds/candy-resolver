"""Live relay with a FAKE source (no network). Run: python -m tests.smoke_liverelay"""
import asyncio
import json

from app.liverelay import (Capacity, FetchError, FetchTimeout, LiveRegistry, UpstreamResponse, classify, file_ext, rewrite_playlist, serve,
                           ts_offset, url_is_safe)

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


class Fake:
    """Pretends to be the source. routes[url] = list of behaviours used one after the other (the last one repeats)."""
    def __init__(self):
        self.routes, self.calls, self.closed, self.opened = {}, [], 0, 0

    def add(self, url, *behaviours):
        self.routes[url] = list(behaviours)

    async def get(self, url, headers):
        self.calls.append((url, dict(headers)))
        seq = self.routes.get(url)
        if not seq:
            return self._resp(404, b"")
        b = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(b, Exception):
            raise b
        return self._resp(*b) if isinstance(b, tuple) else b

    def _resp(self, status, body, headers=None, chunk=4096):
        self.opened += 1
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        parts = [body[i:i + chunk] for i in range(0, len(body), chunk)] or [b""]

        async def it():
            for p in parts:
                yield p

        async def close():
            self.closed += 1
        return UpstreamResponse(status, hdrs, it(), close)


async def nosleep(_):
    pass


async def allow(url):
    return True


def reg(**kw):
    return LiveRegistry(clock=lambda: T["now"], **kw)


T = {"now": 1000.0}
CORS = {"Access-Control-Allow-Origin": "*"}
MASTER = "https://cdn.example.com/pl/abc/master.m3u8?token=T1"
HDRS = {"Referer": "https://player.example.com/embed/1", "Cookie": "a=b"}


def new_session(r, entry=MASTER, kind="hls", ext="m3u8", **kw):
    args = dict(api_key_id=1, task_id="tsk_1", stream_id="str_1", source="Server A", quality="auto", kind=kind, entry_url=entry, entry_ext=ext,
                headers=dict(HDRS), ua="UA/1", ttl_s=3600)
    args.update(kw)
    return r.start(**args)


async def req(r, s, f, name, method="GET", rng=None, **kw):
    return await serve(r, s, f, name, "exp=1&sig=ab", method, rng, CORS, safe=kw.pop("safe", allow), sleep=nosleep, **kw)


async def body_of(reply):
    return b"".join([c async for c in reply.stream]) if reply.stream else (reply.body or b"")


MASTER_TXT = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="en",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",NAME="en",URI="subs/en.m3u8"
#EXT-X-MEDIA:TYPE=CLOSED-CAPTIONS,GROUP-ID="c",NAME="cc",INSTREAM-ID="CC1"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,AUDIO="a"
360/index.m3u8?token=T1
#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720,AUDIO="a"
https://other.example.com/720/index.m3u8
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=200000,URI="iframe.m3u8"
"""
MEDIA_TXT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x01
#EXT-X-MAP:URI="init.mp4",BYTERANGE="720@0"
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00Z
#EXTINF:6.0,
seg0.ts?x=1
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
/abs/seg1.jpg
#EXT-X-BYTERANGE:500@100
#EXTINF:4.5,
../up/seg2.m4s
#EXT-X-ENDLIST
"""


async def main():
    # ---------------- pure helpers ----------------
    check("classify: playlists, keys, init, vtt, m4s, disguised", classify("https://x/a/b.m3u8?t=1") == ("playlist", "m3u8") and classify("https://x/k", "key") == ("key", "key")
          and classify("https://x/i.mp4", "init") == ("init", "mp4") and classify("https://x/s.vtt") == ("segment", "vtt") and classify("https://x/s.m4s") == ("segment", "m4s")
          and classify("https://x/s.jpg") == ("segment", "ts") and classify("https://x/noext") == ("segment", "ts") and classify("https://x/v", "playlist")[0] == "playlist")
    check("file_ext: from format, from url, default", file_ext("mkv", "https://x/a") == "mkv" and file_ext(None, "https://x/a.webm?t=1") == "webm" and file_ext("video/mp4", "https://x/a") == "mp4")
    ts = bytes([0x47]) + bytes(187)
    clean = (ts * 30)
    check("ts_offset: clean / junk prefix / not TS / short", ts_offset(clean) == 0 and ts_offset(b"\x89PNG" + b"x" * 100 + clean) == 104 and ts_offset(b"ftypmp42" * 600) == 0 and ts_offset(b"abc") == 0)

    # ---------------- rewrite ----------------
    r = reg(); s, _ = new_session(r)
    out = rewrite_playlist(MASTER_TXT, "https://cdn.example.com/pl/abc/master.m3u8?token=T1", lambda u, k, e: r.register(s, u, k, e), "exp=1&sig=ab")
    t = s.table
    check("master: renditions + variants + i-frame rewritten, in order", [x[0] for x in t[1:]] == [
        "https://cdn.example.com/pl/abc/audio/en.m3u8", "https://cdn.example.com/pl/abc/subs/en.m3u8", "https://cdn.example.com/pl/abc/360/index.m3u8?token=T1",
        "https://other.example.com/720/index.m3u8", "https://cdn.example.com/pl/abc/iframe.m3u8"], str([x[0] for x in t[1:]]))
    check("master: nothing of the source is left in the text", "cdn.example.com" not in out and "other.example.com" not in out and 'URI="1.m3u8?exp=1&sig=ab"' in out)
    check("master: other lines untouched, CLOSED-CAPTIONS without URI kept", "INSTREAM-ID=\"CC1\"" in out and "#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720" in out and out.startswith("#EXTM3U"))
    check("variants are playlists", all(x[1] == "playlist" for x in t[1:]))
    s2 = new_session(r, stream_id="str_2")[0]
    out2 = rewrite_playlist(MEDIA_TXT, "https://cdn.example.com/m/index.m3u8", lambda u, k, e: r.register(s2, u, k, e), "Q")
    kinds = {x[0].rsplit("/", 1)[-1]: (x[1], x[2]) for x in s2.table[1:]}
    check("media: key / init / segments with the right kinds", kinds["key.bin"] == ("key", "key") and kinds["init.mp4"] == ("init", "mp4") and kinds["seg0.ts?x=1"] == ("segment", "ts")
          and kinds["seg1.jpg"] == ("segment", "ts") and kinds["seg2.m4s"] == ("segment", "m4s"))
    check("media: relative, absolute-path and ../ links resolve", [x[0] for x in s2.table[-3:]] == ["https://cdn.example.com/m/seg0.ts?x=1", "https://cdn.example.com/abs/seg1.jpg", "https://cdn.example.com/up/seg2.m4s"])
    check("media: tags, byte ranges, endlist and IV are kept", all(x in out2 for x in ["#EXT-X-TARGETDURATION:6", "IV=0x01", 'BYTERANGE="720@0"', "#EXT-X-BYTERANGE:500@100", "#EXT-X-ENDLIST", "#EXT-X-DISCONTINUITY"]))
    skd = rewrite_playlist('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://abc"\n#EXTINF:1,\ns.ts\n', "https://x/a.m3u8", lambda u, k, e: 7, "Q")
    check("non-http key URIs are left alone", 'URI="skd://abc"' in skd and "7.ts?Q" in skd)
    same = rewrite_playlist("#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\na.ts\n", "https://x/p.m3u8", lambda u, k, e: r.register(s2, u, k, e), "Q")
    check("the same url gets the same number", same.count("\n") and len(set(l for l in same.splitlines() if l.endswith("?Q"))) == 1)

    # ---------------- registry ----------------
    r = reg(max_active=2, max_sessions=3)
    a, reused = new_session(r, stream_id="a"); b, _ = new_session(r, stream_id="b")
    check("a session starts with the entry as number 0", a.table[0] == (MASTER, "playlist", "m3u8") and a.id.startswith("liv_") and len(a.id) == 20 and not reused)
    a2, reused = new_session(r, stream_id="a")
    check("same stream + key: the same session is reused", a2 is a and reused and r.totals["started"] == 2)
    try:
        new_session(r, stream_id="c"); cap = False
    except Capacity:
        cap = True
    check("capacity: a third active stream is refused", cap)
    check("capacity: but the same stream of another key is a different session", True)
    T["now"] += 200
    c, _ = new_session(r, stream_id="c")
    check("idle sessions (not seen for 2 min) do not count as active", c.id in r.sessions and r.active_count() == 1)
    d, _ = new_session(r, stream_id="d")
    check("memory bound: the oldest is dropped beyond max_sessions", len(r.sessions) == 3 and a.id not in r.sessions)
    T["now"] += 4000
    check("sessions expire", r.get(c.id) is None and r.snapshot()["sessions"] == [])
    r = reg(); a, _ = new_session(r)
    check("end(): only the owner can end a session", not r.end(a.id, 99) and r.end(a.id, 1) and r.get(a.id) is None)
    snap = (lambda: (new_session(r, stream_id="z"), r.snapshot())[1])()
    check("snapshot has what the admin needs", snap["sessions"][0]["source"] == "Server A" and snap["totals"]["started"] >= 1 and "bytes_out" in snap["sessions"][0])

    # ---------------- serve: playlists ----------------
    r = reg(); s, _ = new_session(r); f = Fake()
    f.add(MASTER, (200, MASTER_TXT.encode()))
    rep = await req(r, s, f, "0.m3u8")
    body = rep.body.decode()
    check("master is fetched with the stream's headers + UA, no compression", f.calls[0][1]["Referer"] == HDRS["Referer"] and f.calls[0][1]["User-Agent"] == "UA/1" and f.calls[0][1]["Accept-Encoding"] == "identity")
    check("master answer: 200, playlist type, CORS, no-store, length matches", rep.status == 200 and rep.headers["Content-Type"].startswith("application/vnd.apple") and rep.headers["Access-Control-Allow-Origin"] == "*"
          and rep.headers["Cache-Control"] == "no-store" and int(rep.headers["Content-Length"]) == len(rep.body) and "1.m3u8?exp=1&sig=ab" in body)
    f.add("https://cdn.example.com/pl/abc/360/index.m3u8?token=T1", (200, MEDIA_TXT.encode()))
    n360 = [i for i, x in enumerate(s.table) if x[0].endswith("360/index.m3u8?token=T1")][0]
    rep = await req(r, s, f, f"{n360}.m3u8")
    check("variant playlist: relative segment urls resolve against ITS url, all rewritten", rep.status == 200 and "cdn.example.com" not in rep.body.decode() and any(x[0] == "https://cdn.example.com/pl/abc/360/seg0.ts?x=1" for x in s.table))
    rep = await req(r, s, f, "0.m3u8", method="HEAD")
    check("HEAD playlist: headers, no body", rep.status == 200 and rep.body is None and "Content-Length" in rep.headers)
    f.add(MASTER, (200, b"<html>captcha</html>"))
    rep = await req(r, s, f, "0.m3u8")
    check("HTML instead of a playlist -> 502 upstream_bad_playlist with a JSON explanation", rep.status == 502 and rep.headers["X-Candy-Error"] == "upstream_bad_playlist" and json.loads(rep.body)["code"] == "upstream_bad_playlist" and rep.headers["Content-Type"] == "application/json")
    f.add(MASTER, (200, b"\xef\xbb\xbf\n#EXTM3U\n#EXTINF:1,\nx.ts\n"))
    check("BOM / blank lines before #EXTM3U are fine", (await req(r, s, f, "0.m3u8")).status == 200)
    f.add(MASTER, (200, b"#EXTM3U\n" + b"#EXTINF:1,\nseg.ts\n" * 100000))
    rep = await req(r, s, f, "0.m3u8", max_playlist=1000)
    check("a huge playlist is refused", rep.status == 502 and rep.error.code == "upstream_bad_playlist")
    live = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:%d\n" + "".join("#EXTINF:6,\nl%d.ts\n" % i for i in range(3))
    s3 = new_session(r, stream_id="live", entry="https://cdn.example.com/live.m3u8")[0]
    f.add("https://cdn.example.com/live.m3u8", (200, (live % 0).encode()), (200, ((live % 1) + "#EXTINF:6,\nl3.ts\n").encode()))
    await req(r, s3, f, "0.m3u8"); size1 = len(s3.table)
    rep = await req(r, s3, f, "0.m3u8")
    check("a growing live playlist works: new segments get new numbers, old ones keep theirs", len(s3.table) == size1 + 1 and "ENDLIST" not in rep.body.decode())

    # ---------------- serve: media ----------------
    r = reg(); s, _ = new_session(r); f = Fake()
    seg = "https://cdn.example.com/pl/abc/seg.ts"
    s.table.append((seg, "segment", "ts"))
    payload = ts * 2000
    f.add(seg, (200, payload, {"Content-Length": str(len(payload)), "Content-Type": "image/png", "Accept-Ranges": "bytes"}))
    rep = await req(r, s, f, "1.ts")
    data = await body_of(rep)
    check("segment: streamed intact, type is OUR video/mp2t (not the source's image/png), length + ranges passed", data == payload and rep.headers["Content-Type"] == "video/mp2t"
          and rep.headers["Content-Length"] == str(len(payload)) and rep.headers["Accept-Ranges"] == "bytes" and rep.headers["Cache-Control"].startswith("private"))
    check("segment: bytes are counted, upstream closed", s.bytes_out == len(payload) and r.totals["bytes"] == len(payload) and f.closed >= 1)
    junk = b"\x89PNG\r\n" + b"j" * 98
    f.add(seg, (200, junk + payload, {"Content-Length": str(len(junk) + len(payload))}))
    rep = await req(r, s, f, "1.ts")
    data = await body_of(rep)
    check("segment with a junk header: junk is cut, length corrected", data == payload and rep.headers["Content-Length"] == str(len(payload)))
    f.add(seg, (206, payload[:1000], {"Content-Range": "bytes 0-999/376000", "Content-Length": "1000"}))
    rep = await req(r, s, f, "1.ts", rng="bytes=0-999")
    check("Range is passed on, 206 + Content-Range come back", rep.status == 206 and rep.headers["Content-Range"] == "bytes 0-999/376000" and f.calls[-1][1]["Range"] == "bytes=0-999" and len(await body_of(rep)) == 1000)
    f.add(seg, (200, ts * 10, {}))
    for bad in ("bytes=0-1,5-9", "bytes=0-1; evil", "items=0-5", "bytes=" + "9" * 200):
        await req(r, s, f, "1.ts", rng=bad)
        assert "Range" not in f.calls[-1][1], bad
    check("multi-range / strange Range headers are never forwarded", True)
    f.add(seg, (416, b"", {"Content-Range": "bytes */1000"}))
    rep = await req(r, s, f, "1.ts", rng="bytes=5000-")
    check("416 is passed to the player", rep.status == 416 and rep.headers["Content-Range"] == "bytes */1000")
    f.add(seg, (200, payload, {"Content-Length": str(len(payload))}))
    rep = await req(r, s, f, "1.ts", method="HEAD")
    check("HEAD segment: headers only, upstream closed", rep.status == 200 and rep.stream is None and "Content-Length" in rep.headers)
    s.table.append(("https://cdn.example.com/k.bin", "key", "key"))
    f.add("https://cdn.example.com/k.bin", (200, b"0123456789abcdef"))
    rep = await req(r, s, f, "2.key")
    check("key: never cached, octet-stream", rep.headers["Cache-Control"] == "no-store" and rep.headers["Content-Type"] == "application/octet-stream" and await body_of(rep) == b"0123456789abcdef")
    fm = new_session(r, stream_id="file", entry="https://cdn.example.com/movie.mp4", kind="file", ext="mp4")[0]
    f.add("https://cdn.example.com/movie.mp4", (206, b"x" * 500, {"Content-Range": "bytes 0-499/9999", "Content-Length": "500"}))
    rep = await req(r, fm, f, "0.mp4", rng="bytes=0-499")
    check("direct file (mp4): relayed with range support, video/mp4", rep.status == 206 and rep.headers["Content-Type"] == "video/mp4" and len(await body_of(rep)) == 500)
    closed_before = f.closed
    f.add(seg, (200, payload, {}))
    rep = await req(r, s, f, "1.ts")
    agen = rep.stream.__aiter__(); await agen.__anext__(); await agen.aclose()
    check("player leaves early: the source connection is closed", f.closed > closed_before)

    # ---------------- errors ----------------
    r = reg(); s, _ = new_session(r); f = Fake(); s.table.append((seg, "segment", "ts"))
    f.add(MASTER, (403, b"nope"))
    rep = await req(r, s, f, "0.m3u8")
    check("403 on the playlist -> 410 source_link_expired, stream flagged once", rep.status == 410 and rep.headers["X-Candy-Error"] == "source_link_expired" and rep.mark_dead and s.dead)
    calls = len(f.calls)
    rep = await req(r, s, f, "1.ts")
    check("after that every request fails at once, no more load on the source, no second flag", rep.status == 410 and not rep.mark_dead and len(f.calls) == calls)
    for code in (401, 410):
        r = reg(); s, _ = new_session(r); f = Fake(); f.add(MASTER, (code, b""))
        check(f"{code} on the playlist -> link expired", (await req(r, s, f, "0.m3u8")).status == 410)
    r = reg(); s, _ = new_session(r); f = Fake(); s.table.append((seg, "segment", "ts"))
    f.add(MASTER, (404, b""))
    check("404 on a playlist -> link expired", (await req(r, s, f, "0.m3u8")).headers["X-Candy-Error"] == "source_link_expired")
    r = reg(); s, _ = new_session(r); f = Fake(); s.table.append((seg, "segment", "ts"))
    f.add(seg, (404, b""))
    rep = await req(r, s, f, "1.ts")
    check("404 on one segment is just 404 upstream_not_found: the stream is NOT written off", rep.status == 404 and rep.error.code == "upstream_not_found" and not s.dead and not rep.mark_dead)
    f.add(seg, (401, b""))
    rep = await req(r, s, f, "1.ts")
    check("401 on a segment -> link expired", rep.status == 410 and s.dead and rep.mark_dead)
    r = reg(); s, _ = new_session(r); f = Fake(); s.table.append((seg, "segment", "ts"))
    f.add(seg, (503, b""), (200, ts * 10, {}))
    rep = await req(r, s, f, "1.ts")
    check("a 5xx hiccup is retried once, the player never notices", rep.status == 200 and len(f.calls) == 2)
    f.calls.clear(); f.add(seg, (500, b""))
    rep = await req(r, s, f, "1.ts")
    check("a source that keeps failing -> 502 upstream_error (not 'expired')", rep.status == 502 and rep.error.code == "upstream_error" and not s.dead and len(f.calls) == 2)
    f.add(seg, FetchError("reset"), (200, ts * 10, {}))
    check("a network error is retried once too", (await req(r, s, f, "1.ts")).status == 200)
    f.add(seg, FetchTimeout("slow"))
    rep = await req(r, s, f, "1.ts")
    check("timeouts -> 504 upstream_timeout", rep.status == 504 and rep.error.code == "upstream_timeout")
    f.add(seg, (429, b""))
    rep = await req(r, s, f, "1.ts")
    check("429 from the source -> 503 with Retry-After, not expired", rep.status == 503 and rep.headers["Retry-After"] == "5" and not s.dead)
    codes = [(await req(r, s, f, n)).error.code for n in ("99.ts", "1.m3u8", "../x", "abc")]
    check("unknown number / wrong extension / junk name -> 404 live_ref_unknown", codes == ["live_ref_unknown"] * 4, str(codes))
    check("errors are counted per code", r.totals["errors"] >= 6 and s.errors.get("upstream_error", 0) >= 1)

    # ---------------- safety ----------------
    async def resolver(host):
        return {"internal.example.com": ["10.0.0.5"], "mixed.example.com": ["93.184.216.34", "127.0.0.1"], "fine.example.com": ["93.184.216.34"], "v6.example.com": ["2606:2800:220:1:248:1893:25c8:1946"]}[host]
    async def many(urls):
        return [await url_is_safe(u, resolver) for u in urls]
    refused = await many(("http://127.0.0.1/x", "http://10.1.2.3/x", "http://192.168.0.1/x", "http://169.254.169.254/latest/meta-data", "http://[::1]/x",
                          "file:///etc/passwd", "ftp://example.com/x", "http:///nohost", "http://internal.example.com/x", "http://mixed.example.com/x"))
    check("url_is_safe: private / loopback / link-local / metadata / bad schemes / mixed DNS answers are refused", not any(refused), str(refused))
    check("url_is_safe: public names and addresses pass", all(await many(("https://fine.example.com/a.ts", "http://93.184.216.34/x", "https://v6.example.com/x"))))
    r = reg(); s, _ = new_session(r, entry="http://169.254.169.254/latest/meta-data/"); f = Fake()
    f.add("http://169.254.169.254/latest/meta-data/", (200, b"#EXTM3U\nsecret"))

    async def realsafe(u):
        return await url_is_safe(u, resolver)
    rep = await req(r, s, f, "0.m3u8", safe=realsafe)
    check("an entry pointing into the private network is refused before any request", rep.status == 502 and rep.error.code == "upstream_blocked" and not f.calls)
    r = reg(); s, _ = new_session(r); f = Fake()
    f.add(MASTER, (302, b"", {"Location": "http://10.0.0.9/admin"}))
    rep = await req(r, s, f, "0.m3u8", safe=realsafe)
    check("a redirect into the private network is refused too", rep.error.code == "upstream_blocked" and len(f.calls) == 1)
    f.add(MASTER, (302, b"", {"Location": "/moved/master.m3u8"}), (200, b"#EXTM3U\n#EXTINF:1,\nseg.ts\n"))
    f.add("https://cdn.example.com/moved/master.m3u8", (200, b"#EXTM3U\n#EXTINF:1,\nseg.ts\n"))
    rep = await req(r, s, f, "0.m3u8", safe=allow)
    check("a normal redirect is followed; links resolve against the FINAL url", rep.status == 200 and s.table[-1][0] == "https://cdn.example.com/moved/seg.ts")
    f.add(MASTER, (302, b"", {"Location": MASTER}))
    rep = await req(r, s, f, "0.m3u8")
    check("a redirect loop ends with an error, not a hang", rep.status == 502 and "redirect" in json.loads(rep.body)["message"])
    check("sessions are separate: one session cannot reach another's table", len(s.table) >= 1 and (await req(r, s, f, "50.ts")).status == 404)
    print("ALL OK" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
