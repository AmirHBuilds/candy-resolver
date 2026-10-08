"""In-process wake-ups for tasks: lets long-polling requests react the instant a source finishes.
Purely an optimisation: waiters also re-check the database every second, so a missed or cross-process
signal only costs up to one second (see waiting.py)."""
import asyncio

_events: dict[str, asyncio.Event] = {}


def register(task_id: str) -> None:
    _events[task_id] = asyncio.Event()


def get(task_id: str) -> asyncio.Event | None:
    return _events.get(task_id)


def notify(task_id: str) -> None:
    """Wake everyone waiting on the current event, then install a fresh one for the next change."""
    ev = _events.get(task_id)
    if ev is not None:
        ev.set()
        _events[task_id] = asyncio.Event()


def unregister(task_id: str) -> None:
    ev = _events.pop(task_id, None)
    if ev is not None:
        ev.set()
