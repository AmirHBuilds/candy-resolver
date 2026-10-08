"""Wait for a task to reach a state, without holding a database connection while waiting."""
import asyncio
import time

from . import signals

TERMINAL = ("done", "failed")


def satisfied(st: dict, mode: str, after: int) -> bool:
    if st["status"] in TERMINAL or st.get("expired"):
        return True
    if mode == "first":      # at least one source has streams
        return bool(st["has_streams"])
    if mode == "first_starred":
        if st["has_starred_streams"]:
            return True
        # Fallback so the client is never left waiting for nothing: once every starred source has
        # finished without streams (or there are none), the first source with streams is good enough.
        return bool(st["has_streams"]) and st["starred_done"] >= st["starred_total"]
    if mode == "change":     # something new since the client's last version
        return st["version"] > after
    return False             # "all": only a finished task counts


async def wait_for_task(peek, task_id: str, *, mode: str, after: int = 0, timeout: float) -> dict | None:
    """Returns the latest state as soon as `mode` is satisfied or `timeout` seconds pass.
    `peek(task_id)` returns a small state dict (or None) using its own short-lived DB session."""
    deadline = time.monotonic() + timeout
    while True:
        ev = signals.get(task_id)      # fetched BEFORE reading the state, so a change in between is never lost
        st = await peek(task_id)
        if st is None or satisfied(st, mode, after):
            return st
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return st
        if ev is None:                 # task is not running in this process: just poll
            await asyncio.sleep(min(remaining, 1.0))
        else:
            try:
                await asyncio.wait_for(ev.wait(), min(remaining, 1.0))   # 1s cap = safety net
            except asyncio.TimeoutError:
                pass
