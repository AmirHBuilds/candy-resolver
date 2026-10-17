"""Daily report text, schedule, low-disk state and failing-source streaks. Pure logic. Run: python -m tests.smoke_report"""
from datetime import datetime, timezone

from app.notify import disk_state
from app.report import fmt_bytes, fmt_ms, fmt_uptime, format_report, report_due, source_health
from app.streaks import StreakTracker

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


NOW = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)
data = {"period_h": 24, "version": "1.0.7", "errors_logged": 3,
        "tasks": {"total": 128, "done": 118, "failed": 10},
        "sources": [{"name": "Vertex", "runs": 96, "ok": 84, "empty": 8, "error": 4, "avg_ms": 1200},
                    {"name": "Broken <b>", "runs": 40, "ok": 2, "empty": 3, "error": 35, "avg_ms": None},
                    {"name": "Meh", "runs": 20, "ok": 2, "empty": 18, "error": 0, "avg_ms": 9800}],
        "downloads": {"ready": 14, "ready_bytes": 6_200_000_000, "failed": 2, "other": 1,
                      "reasons": [("source link expired (HTTP 401)", 2)]},
        "library": {"items": 23, "bytes": 18_400_000_000},
        "system": {"uptime_s": 3 * 86400 + 4 * 3600, "load": 0.4, "mem_percent": 62.3, "disk_free": 31_500_000_000, "disk_total": 80_000_000_000},
        "autoclean": {"at": NOW.timestamp() - 6 * 3600, "freed_bytes": 340_000_000, "errors": 0}}
txt = format_report(data, "vps-1", NOW)
check("headline numbers", "Daily report" in txt and "vps-1" in txt and "<b>Tasks:</b> 128 (118 done · 10 failed)" in txt)
check("sources: broken first, with health icons", txt.index("Broken &lt;b&gt;") < txt.index("Meh") < txt.index("Vertex") and "🔴 Broken" in txt and "🟠 Meh" in txt and "🟢 Vertex" in txt)
check("source line: runs, found %, avg time, errors", "Vertex — 96 · 88% · 1.2 s · 4 error(s)" in txt)
check("names are HTML-escaped", "<b>Broken" not in txt.replace("<b>Broken &lt;b&gt;", ""))
check("downloads + failure reason", "14 finished (6.2 GB) · 2 failed · 1 still running" in txt and "2× source link expired (HTTP 401)" in txt)
check("library, disk, server, auto-clean, errors", all(x in txt for x in ["23 item(s), 18.4 GB", "31.5 GB free of 80.0 GB (39% free)", "up 3d 4h", "load 0.4", "RAM 62%", "v1.0.7", "freed 340.0 MB, 6 h ago", "Server errors logged:</b> 3"]))
check("fits in one Telegram message", len(txt) < 4096)
quiet = format_report({**data, "tasks": {"total": 0, "done": 0, "failed": 0}, "sources": [], "errors_logged": 0, "autoclean": None,
                       "downloads": {"ready": 0, "ready_bytes": 0, "failed": 0, "reasons": []}}, "x", NOW)
check("a quiet day still reads well", "No tasks were requested." in quiet and "✅" in quiet and "Last auto-clean" not in quiet)
many = format_report({**data, "sources": [{"name": f"S{i}", "runs": 10 + i, "ok": 9 + i, "empty": 0, "error": 1, "avg_ms": 500} for i in range(30)]}, "x", NOW)
check("only the first 10 sources are listed", many.count("🟢 S") == 10 and "and 20 more" in many)
check("health: red / orange / green", source_health({"runs": 10, "ok": 2, "error": 6}) == "🔴" and source_health({"runs": 10, "ok": 1, "error": 0}) == "🟠"
      and source_health({"runs": 10, "ok": 9, "error": 1}) == "🟢" and source_health({"runs": 2, "ok": 0, "error": 2}) == "🟢")
check("formatters", fmt_bytes(1500) == "1.5 KB" and fmt_bytes(0) == "0 B" and fmt_ms(90) == "90 ms" and fmt_ms(None) == "–" and fmt_uptime(3700) == "1h 1m")

# ---- schedule ----
def due(h, m, off=0.0, last=None, t="08:00"):
    return report_due(datetime(2026, 10, 16, h, m, tzinfo=timezone.utc), t, off, last)
check("before the time: not due", due(7, 59)[0] is False)
check("at the time: due", due(8, 0) == (True, "2026-10-16"))
check("after the time, not sent today: due (late start catches up)", due(15, 30)[0] is True)
check("already sent today: not due", due(9, 0, last="2026-10-16")[0] is False)
check("sent yesterday: due again today", due(8, 5, last="2026-10-15")[0] is True)
check("time zone offset: 08:00 at UTC+3:30 is 04:30 UTC", due(4, 29, 3.5)[0] is False and due(4, 30, 3.5)[0] is True)
check("offset can move the date", due(22, 0, 3.5)[1] == "2026-10-17" and due(1, 0, -5)[1] == "2026-10-15")
check("a broken time string falls back to 08:00", due(8, 0, t="nonsense")[0] is True and due(7, 0, t=None)[0] is False)

# ---- low disk state ----
GB = 1e9
check("below 2 GB -> low", disk_state(int(1.5 * GB), 2.0, False) == "low")
check("above the limit and never flagged -> nothing", disk_state(int(5 * GB), 2.0, False) is None)
check("just above the limit while flagged -> still quiet (no flapping)", disk_state(int(2.1 * GB), 2.0, True) is None)
check("clearly above (20 %) while flagged -> recovered", disk_state(int(2.5 * GB), 2.0, True) == "recovered")
check("threshold 0 = warning off", disk_state(0, 0, False) is None and disk_state(10, 0, True) is None)
check("the limit is yours to choose", disk_state(int(8 * GB), 10, False) == "low" and disk_state(int(8 * GB), 5, False) is None)

# ---- failing-source streaks ----
t = StreakTracker()
res = [t.record("a", True, 3) for _ in range(5)]
check("alerts once when the streak reaches the threshold", [r[0] for r in res] == [None, None, "failing", None, None] and res[2][1] == 3)
check("another source is independent", t.record("b", True, 3) == (None, 1))
check("recovery is reported once, with the length of the streak", t.record("a", False, 3) == ("recovered", 5) and t.record("a", False, 3) == (None, 0))
t.record("a", True, 3); t.record("a", True, 3)
check("a success in between resets the count", t.record("a", False, 3) == (None, 0) and t.record("a", True, 3) == (None, 1))
check("threshold 0 never alerts", all(StreakTracker().record("x", True, 0)[0] is None for _ in range(10)))
t2 = StreakTracker(); [t2.record("a", True, 2) for _ in range(2)]
check("after recovery it can alert again", t2.record("a", False, 2)[0] == "recovered" and [t2.record("a", True, 2)[0] for _ in range(2)] == [None, "failing"])
print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
