"""Source order, 'find N sources' scheduling and source timing stats. Pure logic. Run: python -m tests.smoke_ranking"""
import asyncio
from types import SimpleNamespace as NS

from app.ranked import run_ranked
from app.ranking import order_rows, source_stats

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


def src(i, rank=None, starred=False):
    return (NS(id=i, rank=rank, starred=starred), None)


# ---- order ----
rows = [src(1), src(2, rank=2), src(3, rank=1), src(4, starred=True), src(5, rank=1, starred=True)]
check("rank 1 first, then 2, unranked last (by id)", [r[0].id for r in order_rows(rows)] == [3, 5, 2, 1, 4])
check("starred_first: starred group first, rank inside each group", [r[0].id for r in order_rows(rows, True)] == [5, 4, 3, 2, 1])
check("same rank -> lower id first", [r[0].id for r in order_rows([src(9, 1), src(4, 1)])] == [4, 9])

# ---- stats ----
runs = [("ok", 900), ("error", 30000), ("empty", 1100), ("ok", 1000)]
st = source_stats(runs)
check("response time = latest run", st["last_ms"] == 900 and st["last_status"] == "ok")
check("average ignores failed runs", st["avg_ms"] == 1000, st)
check("found rate counts runs that returned streams", st["found_rate"] == 50, st)
check("no runs -> nothing", source_stats([])["avg_ms"] is None and source_stats([])["last_ms"] is None)
check("only errors -> no average", source_stats([("error", 5000)])["avg_ms"] is None)


# ---- run_ranked ----
async def scenario(results, wanted, ok_so_far=0, delay=0.01):
    """results: {name: (is_ok, seconds)}; returns (order started, order finished, found, never started)"""
    started, finished = [], []

    async def run_one(name):
        started.append(name)
        await asyncio.sleep(results[name][1])
        return name

    async def on_result(name):
        finished.append(name)

    found, left = await run_ranked(list(results), run_one, lambda n: results[n][0], on_result, wanted, ok_so_far)
    return started, finished, found, left


async def main():
    allok = {"A": (True, .02), "B": (True, .01), "C": (True, .01)}
    s, f, found, left = await scenario(allok, wanted=1)
    check("wanted=1: only the first source runs", s == ["A"] and found == 1 and left == 2, f"{s}")

    s, f, found, left = await scenario({"A": (False, .01), "B": (True, .01), "C": (True, .01)}, wanted=1)
    check("wanted=1: next source starts only after the first found nothing", s == ["A", "B"] and f == ["A", "B"] and found == 1 and left == 1, f"{s}")

    s, f, found, left = await scenario(allok, wanted=2)
    check("wanted=2: two start together, the third never", s == ["A", "B"] and found == 2 and left == 1, f"{s}")

    s, f, found, left = await scenario({"A": (False, .01), "B": (False, .01), "C": (True, .01)}, wanted=2)
    check("wanted=2 with failures: keeps going until 2 are found or none are left", s == ["A", "B", "C"] and found == 1 and left == 0, f"{s} found={found}")

    s, f, found, left = await scenario(allok, wanted=None)
    check("wanted=None: everything starts, in order", s == ["A", "B", "C"] and found == 3 and left == 0)

    s, f, found, left = await scenario({"D": (True, .01), "E": (True, .01)}, wanted=2, ok_so_far=1)
    check("'find 1 more' after one was found: exactly one more source runs", s == ["D"] and found == 2 and left == 1, f"{s}")

    s, f, found, left = await scenario({"D": (True, .01)}, wanted=1, ok_so_far=1)
    check("already enough found: nothing starts", s == [] and found == 1 and left == 1)

    s, f, found, left = await scenario({}, wanted=3)
    check("no candidates: returns at once", s == [] and found == 0 and left == 0)

    # an error in the result handler must not leave tasks running
    started = []

    async def slow(name):
        started.append(name)
        await asyncio.sleep(5)
        return name

    async def boom(name):
        raise RuntimeError("db down")

    try:
        await asyncio.wait_for(run_ranked(["A"], lambda n: asyncio.sleep(0, n), lambda n: True, boom), 2)
        check("handler error propagates", False)
    except RuntimeError:
        check("handler error propagates", True)
    task = asyncio.ensure_future(run_ranked(["A", "B"], slow, lambda n: True, boom))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.05)
    check("cancelling stops the running sources", len(started) == 2 and all(t.done() for t in asyncio.all_tasks() if t is not asyncio.current_task()))

    print("ALL OK" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
