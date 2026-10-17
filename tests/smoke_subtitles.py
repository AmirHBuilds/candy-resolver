"""Subtitles: conversion, safety, per-request filters, signed serving, cleanup, and "the video is never affected".
Real HTTP server (good + bad + slow + oversized files) and real ffmpeg. Run: python -m tests.smoke_subtitles"""
import asyncio, datetime as dt, gzip, http.server, os, random, re, shutil, subprocess, tempfile, threading, time
from pathlib import Path
from types import SimpleNamespace

from app import signing
from app.config import settings
from app.hls import has_endlist
from app.itemfiles import item_dir, remove_item_dir
from app.progressive import Params as VParams, run_progressive
from app.subengine import Params, parse_langs, plan_subtitles, run_subtitles, write_atomic
from app.subfetch import FetchError, fetch_blocking, is_public_ip
from app.subserve import handle_sub_request, valid_sub_filename
from app.subtitles import (SubtitleRejected, decode_text, lang_label, normalize_lang, sniff, srt_to_vtt, to_vtt)

TMP = Path(tempfile.mkdtemp(prefix="cr_subs_"))
settings.data_dir = str(TMP / "data")
settings.signing_secret = "test-secret"
ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


# =============================================================================== pure: languages, decoding, conversion
check("lang: codes/names/iso3/regions", [normalize_lang(x) for x in ("en", "eng", "ENG", "English", "en-US", "pt_BR", "fas", "per", "Persian", "farsi", "zh-Hans", "Brazilian Portuguese", "es-419")]
      == ["en", "en", "en", "en", "en", "pt", "fa", "fa", "fa", "fa", "zh", "pt", "es"])
check("lang: unknown / empty / junk -> und", [normalize_lang(x) for x in (None, "", "   ", 5, "xyz", "off")] == ["und"] * 6)
check("labels", lang_label("en") == "English" and lang_label("fa") == "Persian" and lang_label("xx") == "XX" and lang_label("und") == "Unknown")

SRT = ("\ufeff1\r\n00:00:01,000 --> 00:00:03,500\r\nHello <i>world</i>\r\n<font color=\"#ff0000\">Red</font> {\\an8}top\r\n\r\n"
       "2\r\n00:00:04,000 --> 00:00:06,000 X1:100 X2:200\r\nSecond & last\r\n\r\n"
       "3\r\n00:00:07,000 --> 00:00:06,000\r\nbackwards cue is dropped\r\n")
EXPECT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.500\nHello <i>world</i>\nRed top\n\n00:00:04.000 --> 00:00:06.000\nSecond &amp; last\n"
vtt, n = srt_to_vtt(SRT)
check("SRT -> VTT: header, comma->dot, BOM/CRLF, tags, positions, bad cue dropped", vtt == EXPECT and n == 2, repr(vtt[:90]))
glued = "1\n00:00:01,000 --> 00:00:02,000\nA\n2\n00:00:03,000 --> 00:00:04,000\nB\n"
check("SRT without blank lines between cues (number glued on)", srt_to_vtt(glued)[0] == "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nA\n\n00:00:03.000 --> 00:00:04.000\nB\n")
check("SRT short milliseconds", "00:00:01.500 --> 00:00:02.050" in srt_to_vtt("1\n00:00:01,5 --> 00:00:02,05\nx\n")[0])
VTT_IN = "\ufeffWEBVTT\r\n\r\n00:01.000 --> 00:02.000\r\nkept as is\r\n"
check("VTT kept, only BOM/line endings fixed", to_vtt("vtt", VTT_IN)[0] == "WEBVTT\n\n00:01.000 --> 00:02.000\nkept as is\n")

fa = "سلام دنيا، حال شما چطور است؟"      # Arabic yeh: that is what real cp1256 Persian files contain (cp1256 has no U+06CC)
fa_srt = f"1\r\n00:00:01,000 --> 00:00:03,000\r\n{fa}\r\n"
check("cp1256 Persian is decoded correctly when the language is fa", fa in decode_text(fa_srt.encode("cp1256"), "fa")[0] and decode_text(fa_srt.encode("cp1256"), "fa")[1] == "cp1256")
es_srt = "1\n00:00:01,000 --> 00:00:03,000\nEl niño está aquí ¿qué?\n"
check("cp1252 Spanish decoded", "niño está aquí ¿qué?" in decode_text(es_srt.encode("cp1252"), "es")[0])
check("latin-1 is the last resort (never fails)", decode_text(bytes([0x81, 0x8d, 0x41]), "en")[1] in ("cp1252", "latin-1"))
check("utf-16 with BOM and without", "Hello" in decode_text("Hello 1".encode("utf-16"), "en")[0] and "Hello" in decode_text("Hello 1".encode("utf-16-le"), "en")[0])
check("utf-8 stays utf-8", decode_text(fa_srt.encode("utf-8"), "fa")[1] == "utf-8")


def rejects(raw):
    try:
        sniff(raw, "en"); return False
    except SubtitleRejected:
        return True


check("rejected: empty / html / json / binary / plain text / xml",
      all(rejects(x) for x in (b"", b"   \n", b"<!DOCTYPE html><html><body>404 Not Found</body></html>", b'{"error":"nope"}',
                               os.urandom(3000), b"just some words\nand more words", b'<?xml version="1.0"?><tt></tt>')))
check("detected by CONTENT: srt/vtt/ass", [sniff(x, "en")[0] for x in (SRT.encode(), VTT_IN.encode(), b"[Script Info]\nTitle: x\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,D,,0,0,0,,Hi\n")] == ["srt", "vtt", "ass"])
check("a .vtt that is really SRT content is still converted", sniff(b"1\n00:00:01,000 --> 00:00:02,000\nx\n", "en")[0] == "srt")

# =============================================================================== pure: planning + SSRF policy
raw = [{"lang": "eng", "url": "http://x/1.srt"}, {"lang": "English", "url": "http://x/1.srt"},     # same lang+url: dropped
       {"lang": "en", "url": "http://x/2.srt"}, {"lang": "fa", "url": "http://x/fa.srt"}, {"lang": "es", "url": "http://x/es.srt"},
       {"lang": "en", "url": "ftp://x/no.srt"}, {"lang": "en", "url": "javascript:alert(1)"}, {"lang": "en"}, "junk", {"lang": "xyz", "url": "http://x/u.srt"}]
p = plan_subtitles(raw)
check("plan: bad schemes/entries and duplicates dropped, numbered in source order", [(c.n, c.lang) for c in p.candidates] == [(1, "en"), (2, "en"), (3, "fa"), (4, "es"), (5, "und")], str([(c.n, c.lang) for c in p.candidates]))
check("plan: unique labels per language", [c.label for c in p.candidates] == ["English", "English 2", "Persian", "Spanish", "Unknown"])
p = plan_subtitles(raw, allow_langs=parse_langs("en, Persian"))
check("plan: language allow-list (names accepted)", [c.lang for c in p.candidates] == ["en", "en", "fa"])
check("plan: empty allow-list means all", len(plan_subtitles(raw, allow_langs=[]).candidates) == 5)
p = plan_subtitles(raw, max_files=2)
check("plan: max files cap, remainder counted", len(p.candidates) == 2 and p.over_cap == 3)
check("public-address policy", [is_public_ip(x) for x in ("93.184.216.34", "8.8.8.8", "2606:4700::1111")] == [True] * 3 and not any(is_public_ip(x) for x in (
    "127.0.0.1", "10.1.2.3", "192.168.0.5", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1")))
check("sub filenames strict", valid_sub_filename("1.vtt") and valid_sub_filename("12.vtt") and not any(valid_sub_filename(x) for x in (
    "../1.vtt", "1.vtt.tmp", "1.srt", "a.vtt", "1234.vtt", ".vtt", "1.vtt/", "1.VTT", "")))

# =============================================================================== a local server with every kind of file
ASS = (b"[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
       b"Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Hello {\\i1}ass{\\i0}\n")
GOOD_SRT = "1\n00:00:01,000 --> 00:00:03,000\nGood subtitle\n"
FILES = {
    "utf8.srt": SRT.encode("utf-8"), "fa.srt": fa_srt.encode("cp1256"), "es.srt": es_srt.encode("cp1252"),
    "utf16.srt": SRT.encode("utf-16"), "ok.vtt": VTT_IN.encode(), "error.html": b"<!DOCTYPE html><html><body><h1>Error 500</h1></body></html>",
    "garbage.srt": os.urandom(5000), "empty.srt": b"", "ass.ass": ASS, "good.srt": GOOD_SRT.encode(), "copy.srt": GOOD_SRT.encode(),
}
sent = {"chunked_huge": 0, "big_cl": 0}
seen_headers = []


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log_message = lambda *a, **k: None

    def _head(self, status, length=None, extra=None):
        self.send_response(status)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        name = self.path.split("?")[0].lstrip("/")
        seen_headers.append((name, dict(self.headers)))
        try:
            if name in FILES:
                body = FILES[name]; self._head(200, len(body), {"Content-Type": "text/plain"}); self.wfile.write(body)
            elif name == "gzip.srt":
                body = gzip.compress(SRT.encode()); self._head(200, len(body), {"Content-Encoding": "gzip"}); self.wfile.write(body)
            elif name == "needs_header.srt":
                if self.headers.get("X-Test") == "yes":
                    body = GOOD_SRT.encode(); self._head(200, len(body)); self.wfile.write(body)
                else:
                    self._head(403, 0)
            elif name == "redir_ok":
                self._head(302, 0, {"Location": "/utf8.srt"})
            elif name == "redir_internal":
                self._head(302, 0, {"Location": "http://169.254.169.254/latest/meta-data/"})
            elif name == "redir_loop":
                self._head(302, 0, {"Location": "/redir_loop"})
            elif name == "big_cl.srt":                          # announces 5 MB
                self._head(200, 5 * 1024 * 1024)
                for _ in range(80):
                    self.wfile.write(b"x" * 65536); sent["big_cl"] += 65536; time.sleep(0.02)
            elif name == "chunked_huge.srt":                    # 10 MB with NO Content-Length: the cap must cut it off
                self._head(200, None, {"Transfer-Encoding": "chunked"})
                for _ in range(160):
                    self.wfile.write(b"10000\r\n" + b"1\n00:00:01,000 --> 00:00:02,000\n"[:1] * 65536 + b"\r\n"); sent["chunked_huge"] += 65536
                    time.sleep(0.005)
                self.wfile.write(b"0\r\n\r\n")
            elif name == "delayed.srt":                         # valid file, but only after 1 s
                time.sleep(1.0); body = GOOD_SRT.encode(); self._head(200, len(body)); self.wfile.write(body)
            elif name == "slow.srt":                            # valid file, but 1 byte every 0.3 s
                body = (GOOD_SRT * 3).encode(); self._head(200, len(body))
                for i in range(len(body)):
                    self.wfile.write(body[i:i + 1]); self.wfile.flush(); time.sleep(0.3)
            elif name == "video.mp4":
                f = TMP / "video.mp4"; size = f.stat().st_size; start, end, st = 0, size - 1, 200
                r = self.headers.get("Range")
                if r:
                    mm = re.match(r"bytes=(\d+)-(\d*)", r); start = int(mm.group(1)); end = int(mm.group(2)) if mm.group(2) else size - 1; st = 206
                extra = {"Accept-Ranges": "bytes", **({"Content-Range": f"bytes {start}-{end}/{size}"} if st == 206 else {})}
                self._head(st, end - start + 1, extra)
                with open(f, "rb") as fh:
                    fh.seek(start); self.wfile.write(fh.read(end - start + 1))
            else:
                self._head(404, 0)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self): self._head(200, 0)


class Quiet(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address): pass


srv = Quiet(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{srv.server_address[1]}"
loopback = lambda ip: ip == "127.0.0.1"                 # test policy: this machine only (169.254.x etc. stay blocked)
SEQ = iter(range(1, 10_000))


def new_item() -> str:
    iid = "lib_" + f"{next(SEQ):016x}"
    item_dir(iid).mkdir(parents=True)
    return iid


def read_sub(iid, n):
    return (item_dir(iid) / "subs" / f"{n}.vtt").read_text(encoding="utf-8")


async def run(raw, *, iid=None, allow=None, default_headers=None, **kw):
    iid = iid or new_item()
    max_files = kw.pop("max_files", 30)
    plan = plan_subtitles(raw, allow_langs=allow, max_files=max_files)
    prog = []

    async def on_progress(tracks, status):
        prog.append((time.monotonic(), tracks, status))
    params = Params(policy=loopback, timeout_s=kw.pop("timeout_s", 4), max_bytes=kw.pop("max_bytes", 2 * 1024 * 1024), **kw)
    t0 = time.monotonic()
    res = await run_subtitles(plan, item_dir(iid), params, default_headers, on_progress)
    return iid, res, prog, time.monotonic() - t0, plan


def u(name, lang="en", **extra):
    return {"lang": lang, "url": f"{B}/{name}", **extra}


async def main():
    # ------------------------------------------------------------------ fetching + conversion, good and bad together
    raw = [u("utf8.srt", "en"), u("fa.srt", "fa"), u("es.srt", "es"), u("utf16.srt", "de"), u("ok.vtt", "fr"),
           u("error.html", "it"), u("garbage.srt", "ru"), u("empty.srt", "tr"), u("big_cl.srt", "pl"), u("chunked_huge.srt", "nl"),
           u("slow.srt", "ja"), u("nope.srt", "ko"), u("redir_internal", "zh"), u("redir_loop", "sv"), u("gzip.srt", "da"),
           u("redir_ok", "fi"), u("ass.ass", "no")]
    iid, res, prog, dur, plan = await run(raw, timeout_s=2.5)
    got = {t["lang"]: t for t in res.tracks}
    check("good files converted: utf-8 srt, cp1256 srt, cp1252 srt, utf-16 srt, vtt, gzip srt, redirected srt, ass",
          set(got) == {"en", "fa", "es", "de", "fr", "da", "fi", "no"}, str(sorted(got)))
    check("SRT -> exact VTT on disk", read_sub(iid, 1) == EXPECT)
    check("cp1256 Persian came out as utf-8 Persian", fa in read_sub(iid, 2) and read_sub(iid, 2).startswith("WEBVTT"))
    check("cp1252 Spanish came out as utf-8", "niño está aquí ¿qué?" in read_sub(iid, 3))
    check("utf-16 srt converted", "Hello <i>world</i>" in read_sub(iid, 4))
    check("vtt kept", read_sub(iid, 5) == "WEBVTT\n\n00:01.000 --> 00:02.000\nkept as is\n")
    check("gzip-encoded response handled", read_sub(iid, 15) == EXPECT)
    check("redirect followed (to an allowed host)", read_sub(iid, 16) == EXPECT)
    check("ASS/SSA converted by the ffmpeg fallback", "Hello <i>ass</i>" in read_sub(iid, 17) and read_sub(iid, 17).startswith("WEBVTT"))
    why = dict(res.failures)
    check("bad ones skipped: html, garbage, empty", all(k in why for k in (6, 7, 8)), str({k: why[k] for k in (6, 7, 8) if k in why}))
    check("oversized (Content-Length) refused before downloading", "too large" in why.get(9, "") and sent["big_cl"] < 200_000, f"(server sent {sent['big_cl']} bytes)")
    check("oversized with NO Content-Length is cut off WHILE streaming", "too large" in why.get(10, "") and sent["chunked_huge"] < 2 * 1024 * 1024 + 1_500_000, f"(server wrote {sent['chunked_huge'] / 1e6:.1f} MB of 10 MB)")
    check("slow file times out", "timed out" in why.get(11, ""), why.get(11, ""))
    check("404 skipped", "404" in why.get(12, ""))
    check("redirect to an internal address refused", "blocked address" in why.get(13, ""), why.get(13, ""))
    check("redirect loop refused", "redirect" in why.get(14, ""), why.get(14, ""))
    check("status is partial; final progress call matches", res.status == "partial" and prog[-1][2] == "partial" and len(prog[-1][1]) == 8)
    first_ok = min(t for t, tr, st in prog if tr)
    check("the slow subtitle did not hold up the others", first_ok - (prog[-1][0] - dur) < 1.5 and dur < 2.5 + 3, f"(first track after {first_ok - (prog[-1][0] - dur):.2f}s, all done in {dur:.1f}s)")
    check("list grows as files finish (pending updates, then final)", [st for _, _, st in prog][:-1] == ["pending"] * (len(prog) - 1) and len(prog) >= 9 and
          all(len(a[1]) <= len(b[1]) for a, b in zip(prog, prog[1:])))
    check("no temp files left", not list((item_dir(iid) / "subs").glob("*.tmp")))
    check("defaults: none unless asked", not any(t["default"] for t in res.tracks))

    # ------------------------------------------------------------------ per-request language filter, labels, dedupe, default
    two_en = [u("utf8.srt", "en"), u("good.srt", "eng"), u("fa.srt", "fa"), u("es.srt", "es")]
    iid2, res, *_ = await run(two_en, allow=parse_langs(["en", "fa"]))
    check("per-request language filter: only en+fa fetched", sorted((t["lang"], t["n"]) for t in res.tracks) == [("en", 1), ("en", 2), ("fa", 3)] and not (item_dir(iid2) / "subs" / "4.vtt").exists())
    check("two English files get unique labels", [t["label"] for t in res.tracks] == ["English", "English 2", "Persian"] and res.status == "done")
    iid3, res, *_ = await run(two_en, default_lang="fa")
    check("default only when asked: exactly one, the requested language", [t["lang"] for t in res.tracks if t["default"]] == ["fa"])
    iid4, res, *_ = await run([u("good.srt"), u("copy.srt"), u("utf8.srt", "fa")])
    check("same language + same content under a different URL is dropped (and counts as done)", [t["n"] for t in res.tracks] in ([1, 3], [2, 3]) and res.status == "done", str(res.tracks))
    iid5, res, *_ = await run([u("utf8.srt"), u("good.srt", "fa"), u("fa.srt", "ar"), u("es.srt", "es")], max_files=2)
    check("max files per item enforced; the rest makes it partial", len(res.tracks) == 2 and res.status == "partial")
    _, res, *_ = await run([u("error.html"), u("garbage.srt"), u("nope.srt")])
    check("every subtitle fails -> status none, no exception, no tracks", res.status == "none" and not res.tracks and len(res.failures) == 3)
    _, res, *_ = await run([])
    check("source without subtitles -> none", res.status == "none")
    _, res, *_ = await run([u("utf8.srt")])
    check("all fine -> done", res.status == "done")

    # ------------------------------------------------------------------ task-wide subtitles: several sources at once
    multi = [{**u("utf8.srt", "en"), "_source": "Server A"}, {**u("good.srt", "en"), "_source": "Server B"},
             {**u("copy.srt", "eng"), "_source": "Server C"}, {**u("fa.srt", "fa"), "_source": "Server B"}]
    _, res, *_ = await run(multi)
    srcs = [t["source"] for t in res.tracks]
    check("task-wide: every track remembers which provider it came from", all(srcs) and "Server A" in srcs and "Server B" in srcs, str(srcs))
    check("task-wide: identical content from two providers is kept once; labels stay unique", len(res.tracks) == 3 and res.status == "done"
          and [t["label"] for t in res.tracks if t["lang"] == "en"] == ["English", "English 2"], str([(t["n"], t["label"], t["source"]) for t in res.tracks]))
    check("single-stream items have no source on their tracks", all(t["source"] is None for t in (await run([u("utf8.srt")]))[1].tracks))

    # ------------------------------------------------------------------ headers + safety defaults
    _, res, *_ = await run([u("needs_header.srt", "en", headers={"X-Test": "yes"}), u("needs_header.srt", "fa", headers=None)], default_headers={"X-Test": "yes"})
    check("uses the subtitle's own headers, else the stream's headers", len(res.tracks) == 2)
    _, res, *_ = await run([u("needs_header.srt", "en")], default_headers=None)
    check("...and without any headers the server says no", res.status == "none")
    plan = plan_subtitles([u("utf8.srt")]); got_prog = []
    async def keep(t, s): got_prog.append(s)
    res = await run_subtitles(plan, item_dir(new_item()), Params(timeout_s=3), None, keep)       # DEFAULT policy
    check("localhost/internal URLs are refused by default", res.status == "none" and "blocked address" in dict(res.failures).get(1, ""), str(res.failures))
    try:
        fetch_blocking("file:///etc/passwd", None, max_bytes=1000, timeout_s=2); check("file:// refused", False)
    except FetchError as e:
        check("only http/https allowed (file:// refused)", "http" in str(e))
    try:
        fetch_blocking("http://localhost:1/x", None, max_bytes=1000, timeout_s=2); check("localhost refused", False)
    except FetchError as e:
        check("hostname that resolves to loopback is refused", "blocked" in str(e), str(e))
    ff = asyncio.get_event_loop()
    _, res, *_ = await run([u("ass.ass")], ffmpeg_fallback=False)
    check("ASS skipped when the ffmpeg fallback is off", res.status == "none" and "fallback disabled" in dict(res.failures)[1])

    # ------------------------------------------------------------------ signed serving
    iid, res, *_ = await run([u("utf8.srt"), u("fa.srt", "fa")])
    base = settings.library_path
    def item(status="ready", delete_at="future", **kw):
        d = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1) if delete_at == "future" else delete_at
        return SimpleNamespace(id=iid, status=status, delete_at=d, mode="hls", playable=False, **kw)
    def serve(fname, it=None, exp=None, sig=None, **kw):
        e, s = signing.sign(iid, 600)
        return handle_sub_request(item=it if it is not False else None, item_id=iid, base_dir=base, filename=fname,
                                  exp=str(exp if exp is not None else e), sig=sig if sig is not None else s, **kw) if it is not None or True else None
    r = handle_sub_request(item=item(), item_id=iid, base_dir=base, filename="1.vtt", exp=str(signing.sign(iid, 600)[0]), sig=signing.sign(iid, 600)[1])
    check("valid link: 200, text/vtt; charset=utf-8, body = the file", r.status == 200 and r.headers["Content-Type"] == "text/vtt; charset=utf-8" and r.body.decode() == EXPECT)
    check("cache header is private with a max-age", r.headers["Cache-Control"].startswith("private, max-age="))
    e, s = signing.sign(iid, 600)
    def sv(fname, it="x", exp=e, sig=s, **kw):
        return handle_sub_request(item=item() if it == "x" else it, item_id=iid, base_dir=base, filename=fname, exp=str(exp), sig=sig, **kw)
    check("HEAD: headers, no body", sv("1.vtt", method="HEAD").body is None and int(sv("1.vtt", method="HEAD").headers["Content-Length"]) > 0)
    check("bad signature / tampered expiry / expired -> 403", sv("1.vtt", sig="0" * 64).status == 403 and sv("1.vtt", exp=int(e) + 9).status == 403
          and sv("1.vtt", exp=int(time.time()) - 5, sig=signing._mac(iid, int(time.time()) - 5)).status == 403)
    check("non-ASCII signature -> 403, no crash", sv("1.vtt", sig="é" * 64).status == 403)
    check("path traversal / odd names -> 404", {sv(n).status for n in ("../1.vtt", "..%2f1.vtt", "1.vtt/../../x", "1.vtt.tmp", "1.srt", "a.vtt", "1234.vtt", "1.vtt/", "../../etc/passwd", "index.m3u8")} == {404})
    check("missing file -> 404", sv("9.vtt").status == 404)
    check("queued and downloading items serve their finished subtitles", sv("1.vtt", it=item("queued")).status == 200 and sv("1.vtt", it=item("downloading")).status == 200)
    check("failed / expired / deleted / unknown item -> 404", {sv("1.vtt", it=item(s_)).status for s_ in ("failed", "expired", "deleted")} == {404} and sv("1.vtt", it=None).status == 404)
    check("ready but past its delete time -> 404", sv("1.vtt", it=item(delete_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))).status == 404)
    check("signature of another item -> 403", handle_sub_request(item=item(), item_id="lib_ffffffffffffffff", base_dir=base, filename="1.vtt", exp=str(e), sig=s).status == 403)
    c = sv("1.vtt", origin="https://candyflix.example", allowed_origins="https://candyflix.example").headers
    check("CORS like the HLS route", c.get("Access-Control-Allow-Origin") == "https://candyflix.example" and "Content-Range" in c["Access-Control-Expose-Headers"])
    check("CORS: other origins get nothing", "Access-Control-Allow-Origin" not in sv("1.vtt", origin="https://evil.example", allowed_origins="https://candyflix.example").headers)

    # ------------------------------------------------------------------ cleanup: the folder (video + subs) goes as one
    iid6, *_ = await run([u("utf8.srt")])
    (item_dir(iid6) / "video.mp4").write_bytes(b"x" * 100)
    check("subs live inside the item folder", (item_dir(iid6) / "subs" / "1.vtt").is_file())
    remove_item_dir(iid6)
    check("remove_item_dir (used by expiry, DELETE, failed downloads) deletes video AND subtitles", not item_dir(iid6).exists())
    try:
        write_atomic(item_dir(iid6), 1, "WEBVTT\n"); check("writing into a deleted item", False)
    except FileNotFoundError:
        check("a deleted item's folder is never re-created by a late subtitle", not item_dir(iid6).exists())
    # delete the item while subtitles are still downloading: a subtitle that arrives AFTER the delete must not bring the folder back
    iid7 = new_item()
    task = asyncio.create_task(run([u("delayed.srt", "en"), u("slow.srt", "ja")], iid=iid7, timeout_s=3))
    await asyncio.sleep(0.4); remove_item_dir(iid7)
    _, res, *_ = await task
    check("delete while subtitles are still downloading: finishes quietly, nothing re-created", not item_dir(iid7).exists() and res.status == "none"
          and "folder is gone" in dict(res.failures).get(1, ""), str(res.failures))
    remove_item_dir("../../etc"); remove_item_dir("lib_zz")
    check("remove_item_dir ignores odd ids", True)

    # ------------------------------------------------------------------ the VIDEO is never affected
    print("... generating a short video")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "14",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(TMP / "video.mp4")], check=True)
    vid = item_dir(new_item()).parent / "lib_00000000000fffff"
    gaps, stop = [], False

    async def ticker():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0.05); now = time.monotonic(); gaps.append(now - last); last = now

    async def noop(u_): pass
    tk = asyncio.create_task(ticker())
    t0 = time.monotonic()
    video = asyncio.create_task(run_progressive(video_url=f"{B}/video.mp4", headers={}, user_agent="t", dest_dir=vid,
                                                params=VParams(min_segments=2, timeout_s=60, free_bytes=lambda: 10 ** 12, reserve_bytes=0), on_update=noop))
    all_bad = [u("error.html"), u("garbage.srt"), u("nope.srt"), u("slow.srt", "ja"), u("big_cl.srt"), u("chunked_huge.srt"), u("redir_internal")]
    _, sres, _, sdur, _ = await run(all_bad, timeout_s=3)
    vres = await video
    vdone = time.monotonic() - t0
    stop = True; await tk
    check("every subtitle failed (status none) ...", sres.status == "none" and not sres.tracks)
    check("... and the video still completed: playlist has ENDLIST", vres["segments"] >= 3 and has_endlist((vid / "index.m3u8").read_text()), f"({vres['segments']} segments in {vdone:.1f}s)")
    check("the video finished while subtitles were still failing (not after them)", vdone < sdur + 1.0 or vdone < 6, f"(video {vdone:.1f}s, subtitles {sdur:.1f}s)")
    check("the event loop never stalled (max gap)", max(gaps) < 0.5, f"({max(gaps) * 1000:.0f} ms)")
    remove_item_dir(vid.name)

    srv.shutdown(); shutil.rmtree(TMP, ignore_errors=True)
    print("\nALL PASSED" if ok else "\nSOME FAILED")


asyncio.run(main())
