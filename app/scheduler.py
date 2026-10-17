"""Admission control: keeps a small server from running out of memory.

* Tasks: at most MAX_CONCURRENT_TASKS run at once. The rest wait in a first-in-first-out line (status "queued",
  with a queue_position the API shows). 0 = no limit.
* Browser scripts: at most MAX_BROWSER_SCRIPTS browser-based source scripts run at once, across ALL tasks.
  Light (plain HTTP) sources are never held back by this.
"""
import asyncio
from contextlib import asynccontextmanager, nullcontext

from .config import settings


class TaskQueue:
    def __init__(self) -> None:
        self._sem: asyncio.Semaphore | None = None
        self._waiting: list[str] = []
        self.active: set[str] = set()
        self._inflight = 0            # holding a slot + waiting for one (counted synchronously, before any await)

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:                       # created lazily: the limit is read once (restart to change it)
            self._sem = asyncio.Semaphore(settings.max_concurrent_tasks)
        return self._sem

    @property
    def limit(self) -> int:
        return settings.max_concurrent_tasks

    def full(self) -> bool:
        return self.limit > 0 and len(self._waiting) >= settings.max_queued_tasks

    def position(self, task_id: str) -> int | None:
        """1 = next in line. None = not waiting (running, finished, or unknown)."""
        try:
            return self._waiting.index(task_id) + 1
        except ValueError:
            return None

    @asynccontextmanager
    async def slot(self, task_id: str, on_queued=None):
        """Hold a run slot for the duration of the block. If none is free, wait in line (calling on_queued once)."""
        if self.limit <= 0:
            self.active.add(task_id)
            try:
                yield
            finally:
                self.active.discard(task_id)
            return
        sem = self._semaphore()
        must_wait = self._inflight >= self.limit      # decided and counted in one step: no await in between
        self._inflight += 1
        try:
            if must_wait:
                self._waiting.append(task_id)
            acq = asyncio.ensure_future(sem.acquire())     # joins the semaphore's FIFO line right away
            try:
                if must_wait and on_queued:
                    await on_queued()
                await acq
            except BaseException:
                if acq.done() and not acq.cancelled() and acq.exception() is None:
                    sem.release()                           # we got the slot but are leaving: give it back
                else:
                    acq.cancel()
                raise
            finally:
                if task_id in self._waiting:
                    self._waiting.remove(task_id)
            self.active.add(task_id)
            try:
                yield
            finally:
                self.active.discard(task_id)
                sem.release()
        finally:
            self._inflight -= 1


class BrowserSlots:
    def __init__(self) -> None:
        self._sem: asyncio.Semaphore | None = None
        self.running = 0
        self.waiting = 0

    @property
    def limit(self) -> int:
        return settings.max_browser_scripts

    @asynccontextmanager
    async def slot(self):
        if self.limit <= 0:
            self.running += 1
            try:
                yield
            finally:
                self.running -= 1
            return
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.limit)
        self.waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self.waiting -= 1
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self._sem.release()


queue = TaskQueue()
browser = BrowserSlots()


def source_slot(uses_browser: bool):
    """The context manager a source run must hold: a browser slot for heavy sources, nothing for light ones."""
    return browser.slot() if uses_browser else nullcontext()


def stats() -> dict:
    return {"task_limit": queue.limit, "tasks_running": len(queue.active), "tasks_waiting": len(queue._waiting),
            "queue_cap": settings.max_queued_tasks,
            "browser_limit": browser.limit, "browser_running": browser.running, "browser_waiting": browser.waiting}
