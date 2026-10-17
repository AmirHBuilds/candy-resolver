"""Per-link expiry and dead-link detection. Pure logic. Run: python -m tests.smoke_links"""
from datetime import datetime, timedelta, timezone

from app.linkexpiry import dead_link_reason, link_expiry
from app.taskstate import results_view, stream_alive

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


# the real link from the Vertex source (JWT token, valid 4 hours)
tok = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJteS1hdXRoIiwiaWF0IjoxNzkxODQ2NzE1LCJuYmYiOjE3OTE4NDY3MTUsImV4cCI6MTc5MTg2MTExNSwiaXBfY2lkciI6IjE3Mi4yMzMuNTEuMC8yNCJ9.GSl-1ok1eowzYuG6rDIwy6ZOllHxjP0fyMzJG-qwX9E"
e = link_expiry(f"https://amalgamanomaly.space/pl/abc/master.m3u8?token={tok}")
check("JWT exp is read from ?token=", e == datetime.fromtimestamp(1791861115, tz=timezone.utc), str(e))
check("works for any query name holding a JWT", link_expiry(f"https://x.com/a.m3u8?auth={tok}") == e)
check("JWT in a header (Bearer)", link_expiry("https://x.com/a.m3u8", {"Authorization": f"Bearer {tok}"}) == e)
check("plain epoch ?expires=", link_expiry("https://x.com/a.mp4?expires=1791861115") == e)
check("epoch in milliseconds", link_expiry("https://x.com/a.mp4?exp=1791861115000") == e)
check("earliest wins", link_expiry("https://x.com/a.mp4?expires=1791861116&token=" + tok) == e)
check("no expiry in the link -> None", link_expiry("https://x.com/video/12345/index.m3u8") is None)
check("random numbers are not expiries", link_expiry("https://x.com/a.mp4?id=42&e=7&size=1234567890123456") is None)
check("garbage token is ignored", link_expiry("https://x.com/a.m3u8?token=aaa.bbb.ccc") is None)

# which download errors mean the source refused the link
for text, want in [
    ("Client error '401 Unauthorized' for url 'https://x/y'", "HTTP 401"),
    ("Server returned 403 Forbidden (access denied)", "HTTP 403"),
    ("HTTP error 404 Not Found", "HTTP 404"),
    ("diagnosis: first segment -> HTTP 401, content-type text/html", "HTTP 401"),
    ("Server returned 410 Gone", "HTTP 410"),
    ("[Errno -3] Temporary failure in name resolution", None),
    ("file exceeds the size limit", None),
    ("download incomplete (got 404 of 5000 bytes)", None),
    ("Server returned 500 Internal Server Error", None),
    ("", None), (None, None),
]:
    check(f"dead_link_reason({text!r})", dead_link_reason(text) == want, str(dead_link_reason(text)))

# per-link life inside a task
now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
task_end = now + timedelta(hours=20)
h = lambda n: now + timedelta(hours=n)
check("link past its expiry is not alive", not stream_alive(h(-1), False, now))
check("dead link is not alive even if the token is fresh", not stream_alive(h(3), True, now))
check("link without expiry is alive", stream_alive(None, False, now))
n, last = results_view([(h(-1), False), (h(3), False)], task_end, now)
check("one expired + one live: task still usable, ends when the live one ends", n == 1 and last == h(3), f"{n} {last}")
n, last = results_view([(h(-1), False), (h(2), True)], task_end, now)
check("all expired or dead -> nothing alive", n == 0 and last is None)
n, last = results_view([(h(100), False)], task_end, now)
check("a link can never outlive the task", last == task_end)
n, last = results_view([(None, False), (h(3), False)], task_end, now)
check("link without expiry lives as long as the task", n == 2 and last == task_end)
n, last = results_view([(h(-5), False), (None, True)], task_end, now)
check("naive datetimes are treated as UTC", results_view([(h(3).replace(tzinfo=None), False)], task_end.replace(tzinfo=None), now)[0] == 1)

print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
