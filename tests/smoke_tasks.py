"""Tasks keep existing after their results expire. Pure logic checks. Run: python -m tests.smoke_tasks"""
from datetime import datetime, timedelta, timezone

from app.taskstate import effective_status, results_expired
from app.waiting import TERMINAL, satisfied

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
past, future = now - timedelta(hours=1), now + timedelta(hours=1)

check("finished task before its results expire keeps its status", effective_status("done", future, now) == "done" and effective_status("failed", future, now) == "failed")
check("done/failed after expiry -> 'expired' (even if the cleanup job has not run yet)", effective_status("done", past, now) == "expired" and effective_status("failed", past, now) == "expired")
check("already expired stays expired", effective_status("expired", past, now) == "expired")
check("a running/pending task is never shown as expired", effective_status("running", past, now) == "running" and effective_status("pending", past, now) == "pending")
check("exact moment of expiry counts as expired", results_expired("done", now, now))
check("naive datetimes (SQLite) are treated as UTC", effective_status("done", (past).replace(tzinfo=None), now) == "expired" and effective_status("done", future.replace(tzinfo=None), now) == "done")
check("'expired' is a final state for clients polling a task", "expired" in TERMINAL and "done" in TERMINAL and "failed" in TERMINAL and "running" not in TERMINAL)
for mode in ("first", "first_starred", "all", "change"):
    st = {"status": "expired", "version": 1, "has_streams": False, "has_starred_streams": False, "starred_done": 0, "starred_total": 0}
    check(f"long-poll/wait returns at once for an expired task (mode={mode})", satisfied(st, mode, 99))
st = {"status": "running", "version": 1, "has_streams": False, "has_starred_streams": False, "starred_done": 0, "starred_total": 0, "expired": True}
check("a time-expired state also releases waiters", satisfied(st, "all", 0))

print("\nALL PASSED" if ok else "\nSOME FAILED")
