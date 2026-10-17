"""Which source runs get refreshed. Pure logic. Run: python -m tests.smoke_refresh"""
from datetime import datetime, timedelta, timezone

from app.refresh import select_runs

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
H = lambda n: NOW + timedelta(hours=n)
runs = [
    {"id": 1, "label": "Vertex", "source_id": 10, "status": "ok", "streams": [(H(3), False), (H(3), False)]},
    {"id": 2, "label": "Server 11", "source_id": 11, "status": "error", "streams": []},
    {"id": 3, "label": "Archive", "source_id": 12, "status": "ok", "streams": [(H(-1), False), (None, True)]},   # all unusable
    {"id": 4, "label": "Empty one", "source_id": 13, "status": "empty", "streams": []},
    {"id": 5, "label": "Mixed", "source_id": 14, "status": "ok", "streams": [(H(-1), False), (H(2), False)]},     # one still usable
    {"id": 6, "label": "Vertex", "source_id": 15, "status": "ok", "streams": [(None, False)]},                     # same public name as #1
]
ids = lambda sel: sorted(r["id"] for r in sel[0])

check("by name: one source", ids(select_runs(runs, ["Archive"], False, False, NOW)) == [3])
check("by name: several sources, any order", ids(select_runs(runs, ["Archive", "Server 11"], False, False, NOW)) == [2, 3])
check("names are case-insensitive and trimmed", ids(select_runs(runs, ["  archive "], False, False, NOW)) == [3])
check("a name shared by two sources refreshes both", ids(select_runs(runs, ["vertex"], False, False, NOW)) == [1, 6])
sel, unknown = select_runs(runs, ["Archive", "Nope"], False, False, NOW)
check("unknown names are reported, the known ones still selected", unknown == ["Nope"] and [r["id"] for r in sel] == [3])
check("expired: only sources with NO usable link left", ids(select_runs(runs, [], True, False, NOW)) == [3])
check("failed: only sources whose run errored (not 'empty')", ids(select_runs(runs, [], False, True, NOW)) == [2])
check("combined: names + expired + failed, no duplicates", ids(select_runs(runs, ["Archive", "Vertex"], True, True, NOW)) == [1, 2, 3, 6])
check("nothing asked -> nothing selected", select_runs(runs, [], False, False, NOW) == ([], []))
check("a source that found nothing is not 'expired'", 4 not in ids(select_runs(runs, [], True, True, NOW)))
check("no runs at all", select_runs([], ["x"], True, True, NOW) == ([], ["x"]))
print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
