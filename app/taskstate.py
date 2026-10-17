"""What a task looks like to API clients once its RESULTS have expired. Pure logic, no database.
A task row is kept (for a long time); only its results (stream links, original subtitle links) expire."""
from datetime import datetime, timezone

FINISHED = ("done", "failed", "expired")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def results_expired(status: str, expires_at: datetime, now: datetime | None = None) -> bool:
    """True once a finished task is past its results lifetime (even before the cleanup job has run)."""
    return status in FINISHED and _aware(expires_at) <= (now or datetime.now(timezone.utc))


def effective_status(status: str, expires_at: datetime, now: datetime | None = None) -> str:
    return "expired" if results_expired(status, expires_at, now) else status


def stream_alive(expires_at: datetime | None, dead: bool, now: datetime | None = None) -> bool:
    """A stream link still worth offering: not marked dead by a failed download and not past its own expiry."""
    return not dead and (expires_at is None or _aware(expires_at) > (now or datetime.now(timezone.utc)))


def results_view(streams: list[tuple[datetime | None, bool]], task_expires: datetime,
                 now: datetime | None = None) -> tuple[int, datetime | None]:
    """streams = [(expires_at or None, dead)]. Returns (how many are still alive, when the last of them stops working).
    A link with no readable expiry lives as long as the task; no link outlives the task."""
    now = now or datetime.now(timezone.utc)
    cap = _aware(task_expires)
    ends = [min(_aware(e), cap) if e else cap for e, dead in streams if stream_alive(e, dead, now)]
    return len(ends), (max(ends) if ends else None)
