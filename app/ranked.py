"""Run candidates in priority order, starting only as many as are still needed. No database, no web framework.

wanted=None: start all of them at once, in order (the caller's own limit decides how many really run in parallel).
wanted=N:    keep (found + running) <= N. So N=1 tries the first candidate; only if that one finds nothing does the
             next one start. Stops as soon as N were found."""
import asyncio
from typing import Awaitable, Callable


async def run_ranked(candidates: list, run_one: Callable[[object], Awaitable], is_ok: Callable[[object], bool],
                     on_result: Callable[[object], Awaitable], wanted: int | None = None, ok_so_far: int = 0) -> tuple[int, int]:
    """Returns (found in total, candidates never started)."""
    waiting = list(candidates)
    inflight: set[asyncio.Future] = set()
    found = ok_so_far
    try:
        while True:
            while waiting and (wanted is None or found + len(inflight) < wanted):
                inflight.add(asyncio.ensure_future(run_one(waiting.pop(0))))
            if not inflight:
                break
            done, inflight = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                res = fut.result()
                if is_ok(res):
                    found += 1
                await on_result(res)
    finally:
        for f in inflight:
            f.cancel()
    return found, len(waiting)
