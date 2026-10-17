"""Schedule of the automatic clean. Pure logic. Run: python -m tests.smoke_autoclean"""
import asyncio
import tempfile
from pathlib import Path

from app.autoclean import AutoClean, low_disk, run_loop

ok = True
GB = 1 << 30


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


p = Path(tempfile.mkdtemp()) / "autoclean.json"
a = AutoClean(p); a.load()
check("defaults: on, every 6 h, database tidy on, emergency on", a.cfg["enabled"] and a.cfg["every_hours"] == 6 and a.cfg["database"] and a.cfg["emergency"])
T = 1_000_000.0
big = (500 * GB, 1000 * GB)
check("never ran -> due (first run)", a.due(T, *big) == "scheduled")
a.record("scheduled", {"freed_bytes": 5, "removed": 2, "errors": 0, "database": {"purged_tasks": 1}, "duration_s": 0.5}, now=T)
check("right after a run -> not due", a.due(T + 60, *big) is None)
check("5h59m later -> not due", a.due(T + 6 * 3600 - 60, *big) is None)
check("6 h later -> due", a.due(T + 6 * 3600, *big) == "scheduled")
check("nearly full disk, but the last run was minutes ago -> wait", a.due(T + 300, 2 * GB, 1000 * GB) is None)
check("nearly full disk and 15+ min since the last run -> extra run", a.due(T + 1000, 2 * GB, 1000 * GB) == "low disk")
a.update({"emergency": False})
check("emergency off -> only the schedule", a.due(T + 1000, 2 * GB, 1000 * GB) is None)
a.update({"enabled": False})
check("disabled -> never, not even when 'never ran'", a.due(T + 99 * 3600, *big) is None)
a.update({"enabled": True, "every_hours": 1})
check("every_hours is respected", a.due(T + 3600, *big) == "scheduled")

b = AutoClean(p); b.load()
check("settings and last result survive a restart", b.cfg["every_hours"] == 1 and b.cfg["last"]["freed_bytes"] == 5 and b.cfg["last_run"] == T and b.cfg["emergency"] is False)
pub = b.public(T + 600, {"free": 1})
check("public(): next run in the right number of seconds", pub["next_in_s"] == 3000 and pub["last"]["trigger"] == "scheduled" and pub["disk"] == {"free": 1})
check("public(): never ran -> next_in 0; disabled -> None", AutoClean(Path(tempfile.mkdtemp()) / "x.json").public(T)["next_in_s"] == 0 and (b.update({"enabled": False}) or b.public(T)["next_in_s"] is None))
(p.parent / "bad.json").write_text("{not json")
c = AutoClean(p.parent / "bad.json"); c.load()
check("a corrupt settings file falls back to the defaults", c.cfg["every_hours"] == 6)
check("low_disk(): < 1 GB or < 7 %", low_disk(GB // 2, 10 * GB) and low_disk(60 * GB, 1000 * GB) and not low_disk(200 * GB, 1000 * GB) and not low_disk(0, 0))


async def main():
    st = AutoClean(Path(tempfile.mkdtemp()) / "l.json"); st.load()
    clock = {"t": 5000.0}
    runs, sleeps = [], []

    async def fake_sleep(s):
        sleeps.append(s); clock["t"] += s
        if len(sleeps) > 400:
            raise asyncio.CancelledError

    async def job(reason):
        runs.append((reason, clock["t"]))
        if len(runs) == 1:
            raise RuntimeError("first run blows up")
        st.record(reason, {"freed_bytes": 1}, now=clock["t"])

    try:
        await run_loop(st, job, lambda: (500 * GB, 1000 * GB), first_delay=600, tick=60, clock=lambda: clock["t"], sleep=fake_sleep)
    except asyncio.CancelledError:
        pass
    check("loop waits before the first check", sleeps[0] == 600)
    check("a job that raises does not kill the loop (it is retried)", len(runs) >= 3 and runs[0][0] == "scheduled", str(runs[:3]))
    gaps = [b[1] - a_[1] for a_, b in zip(runs[1:], runs[2:])]
    check("after a successful run the next one comes ~6 h later", gaps and all(abs(g - 6 * 3600) < 120 for g in gaps), str(gaps[:3]))
    print("ALL OK" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
