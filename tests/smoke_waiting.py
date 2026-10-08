"""Checks for wait=first / long polling, using a simulated task (no database needed).
Run: python -m tests.smoke_waiting"""
import asyncio
import time

from app import signals
from app.waiting import wait_for_task

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


class FakeTask:
    """Mimics what the real runner does: a source finishes -> version+1 -> commit -> signals.notify."""
    def __init__(self, tid, finish_times, ok_flags, notify=True, starred=None):
        starred = starred or [False] * len(finish_times)
        self.id, self.notify = tid, notify
        self.state = {"status": "running", "version": 0, "has_streams": False, "has_starred_streams": False,
                      "starred_done": 0, "starred_total": sum(starred), "api_key_id": 1, "expired": False}
        self.finish_times, self.ok_flags, self.starred = finish_times, ok_flags, starred
        if notify:
            signals.register(tid)

    async def peek(self, tid, delay=0.0):
        snap = dict(self.state)
        if delay:
            await asyncio.sleep(delay)        # slow DB: the answer may be stale when it arrives
        return snap

    async def run(self):
        t0 = time.monotonic()
        for i, (t, good) in enumerate(zip(self.finish_times, self.ok_flags)):
            await asyncio.sleep(max(0, t - (time.monotonic() - t0)))
            self.state["version"] += 1
            self.state["has_streams"] = self.state["has_streams"] or good
            if self.starred[i]:
                self.state["starred_done"] += 1
                self.state["has_starred_streams"] = self.state["has_starred_streams"] or good
            if i == len(self.finish_times) - 1:
                self.state["status"] = "done"
            if self.notify:
                signals.notify(self.id)
        if self.notify:
            signals.unregister(self.id)


async def timed(coro):
    t0 = time.monotonic()
    r = await coro
    return r, time.monotonic() - t0


async def main():
    # --- first: answers when the FIRST source with streams arrives, others keep running ---
    t = FakeTask("a", [0.2, 0.6, 1.4], [True, True, False]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "a", mode="first", timeout=5))
    check("wait=first returns at the first good source", 0.15 < dt < 0.5 and r["status"] == "running" and r["has_streams"], f"({dt:.2f}s)")
    r, dt = await timed(wait_for_task(t.peek, "a", mode="all", timeout=5))
    check("wait=all returns when everything is done", 1.0 < dt < 1.9 and r["status"] == "done", f"({dt:.2f}s)")
    await bg

    # --- first: a source that finds nothing does not count; a later one does ---
    t = FakeTask("b", [0.2, 0.5], [False, True]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "b", mode="first", timeout=5))
    check("empty source is skipped, waits for one with streams", 0.4 < dt < 0.9 and r["has_streams"], f"({dt:.2f}s)")
    await bg

    # --- first, but nothing ever has streams: returns when the task finishes ---
    t = FakeTask("c", [0.2, 0.4], [False, False]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "c", mode="first", timeout=5))
    check("no streams anywhere: returns when task is done", 0.3 < dt < 0.8 and r["status"] == "done" and not r["has_streams"], f"({dt:.2f}s)")
    await bg

    # --- long polling with ?after= : one answer per change ---
    t = FakeTask("d", [0.2, 0.6, 1.2], [True, True, True]); bg = asyncio.create_task(t.run())
    seen, after, t0 = [], 0, time.monotonic()
    while True:
        r = await wait_for_task(t.peek, "d", mode="change", after=after, timeout=5)
        seen.append((r["version"], round(time.monotonic() - t0, 1)))
        after = r["version"]
        if r["status"] == "done":
            break
    check("long poll wakes once per source", [v for v, _ in seen] == [1, 2, 3], str(seen))
    check("long poll wakes at the right moments", abs(seen[0][1] - 0.2) < 0.2 and abs(seen[1][1] - 0.6) < 0.2 and abs(seen[2][1] - 1.2) < 0.2)
    await bg

    # --- timeout: returns the current state, not an error ---
    t = FakeTask("e", [5.0], [True]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "e", mode="change", after=0, timeout=0.5))
    check("timeout returns current state on time", 0.4 < dt < 0.8 and r["status"] == "running" and r["version"] == 0, f"({dt:.2f}s)")
    r, dt = await timed(wait_for_task(t.peek, "e", mode="all", timeout=0))
    check("timeout=0 answers immediately", dt < 0.1, f"({dt:.3f}s)")
    bg.cancel(); signals.unregister("e")

    # --- already finished task: every mode returns at once ---
    t = FakeTask("f", [0.05], [True]); await t.run()
    for mode in ("first", "all", "change"):
        r, dt = await timed(wait_for_task(t.peek, "f", mode=mode, after=99, timeout=5))
        check(f"finished task, mode={mode}: immediate", dt < 0.1 and r["status"] == "done", f"({dt:.3f}s)")

    # --- no lost wake-up: a slow DB read returns a STALE snapshot while a source finishes ---
    t = FakeTask("g", [0.1, 3.0], [True, True]); bg = asyncio.create_task(t.run())
    async def slow_peek(tid):
        return await t.peek(tid, delay=0.25)
    r, dt = await timed(wait_for_task(slow_peek, "g", mode="first", timeout=5))
    check("change during a slow read is not lost", dt < 0.9 and r["has_streams"], f"({dt:.2f}s, would be >1s if the wake-up were lost)")
    bg.cancel(); signals.unregister("g")

    # --- safety net: the change happens in ANOTHER process (no signal reaches us) ---
    t = FakeTask("h", [0.3], [True], notify=False); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "h", mode="first", timeout=5))
    check("without signals it still notices within ~1s", 0.25 < dt < 1.4 and r["has_streams"], f"({dt:.2f}s)")
    await bg

    # --- many clients waiting on one task all get released ---
    t = FakeTask("i", [0.3], [True]); bg = asyncio.create_task(t.run())
    res = await asyncio.gather(*[timed(wait_for_task(t.peek, "i", mode="first", timeout=5)) for _ in range(200)])
    check("200 simultaneous waiters all wake together", all(r["has_streams"] and dt < 0.8 for r, dt in res), f"(slowest {max(dt for _, dt in res):.2f}s)")
    await bg

    # --- first_starred: ignores a faster non-starred source ---
    t = FakeTask("s1", [0.2, 0.6], [True, True], starred=[False, True]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "s1", mode="first_starred", timeout=5))
    check("first_starred waits for the starred source, not the faster one", 0.5 < dt < 0.9 and r["has_starred_streams"], f"({dt:.2f}s)")
    await bg
    t = FakeTask("s2", [0.2, 0.6], [True, True], starred=[False, True]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "s2", mode="first", timeout=5))
    check("plain first still returns the faster non-starred one", dt < 0.4, f"({dt:.2f}s)")
    await bg

    # --- first_starred fallback: starred source finds nothing -> first source with streams ---
    t = FakeTask("s3", [0.3, 0.6], [False, True], starred=[True, False]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "s3", mode="first_starred", timeout=5))
    check("starred finished empty: falls back to a source with streams", 0.5 < dt < 0.9 and r["has_streams"], f"({dt:.2f}s)")
    await bg
    t = FakeTask("s4", [0.2, 0.5], [True, False], starred=[False, True]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "s4", mode="first_starred", timeout=5))
    check("fallback fires as soon as the last starred source gives up", 0.4 < dt < 0.8, f"({dt:.2f}s)")
    await bg
    t = FakeTask("s5", [0.3], [True], starred=[False]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "s5", mode="first_starred", timeout=5))
    check("no starred sources at all: behaves like first", dt < 0.6 and r["has_streams"], f"({dt:.2f}s)")
    await bg
    t = FakeTask("s6", [0.2, 5.0], [True, True], starred=[False, True]); bg = asyncio.create_task(t.run())
    r, dt = await timed(wait_for_task(t.peek, "s6", mode="first_starred", timeout=0.6))
    check("slow starred source: times out with current state", 0.5 < dt < 0.9 and r["status"] == "running", f"({dt:.2f}s)")
    bg.cancel(); signals.unregister("s6")

    # --- unknown task ---
    r = await wait_for_task(lambda tid: asyncio.sleep(0, None), "zzz", mode="all", timeout=1)
    check("unknown task returns None", r is None)
    check("no leaked signal entries", not signals._events, str(list(signals._events)))

    print("\nALL PASSED" if ok else "\nSOME FAILED")

asyncio.run(main())
