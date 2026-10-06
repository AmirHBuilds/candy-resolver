"""Checks for signing, range parsing and extension picking. Run: python -m tests.smoke_units"""
import time

from app import signing
from app.config import settings
from app.fileutil import pick_ext
from app.rangeutil import RangeNotSatisfiable, parse_range

settings.signing_secret = "test-secret"
ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name)


exp, sig = signing.sign("lib_1", 60)
check("valid signature", signing.verify("lib_1", exp, sig))
check("wrong item rejected", not signing.verify("lib_2", exp, sig))
check("tampered exp rejected", not signing.verify("lib_1", exp + 100, sig))
check("tampered sig rejected", not signing.verify("lib_1", exp, "0" * 64))
check("expired rejected", not signing.verify("lib_1", int(time.time()) - 5, signing._mac("lib_1", int(time.time()) - 5)))
check("garbage exp rejected", not signing.verify("lib_1", "abc", sig))

check("range 0-99", parse_range("bytes=0-99", 1000) == (0, 99))
check("range open end", parse_range("bytes=500-", 1000) == (500, 999))
check("range suffix", parse_range("bytes=-100", 1000) == (900, 999))
check("range end clamped", parse_range("bytes=900-5000", 1000) == (900, 999))
check("range garbage ignored", parse_range("items=1-2", 1000) is None)
try:
    parse_range("bytes=2000-3000", 1000)
    check("range past end -> 416", False)
except RangeNotSatisfiable:
    check("range past end -> 416", True)

check("ext from format", pick_ext("mkv", "https://x/y") == "mkv")
check("ext from url", pick_ext(None, "https://x/a/movie.webm?x=1") == "webm")
check("ext fallback", pick_ext("hls", "https://x/y") == "mp4")

print("\nALL PASSED" if ok else "\nSOME FAILED")
