"""Which of a task's source runs should be refreshed? Pure logic, no database."""
from datetime import datetime

from .taskstate import stream_alive


def select_runs(runs: list[dict], names: list[str], expired: bool, failed: bool, now: datetime) -> tuple[list[dict], list[str]]:
    """runs = [{"id", "label", "source_id", "status", "streams": [(expires_at | None, dead)]}] (label = the name clients see).
    names: refresh these sources (case-insensitive; a name used by several sources refreshes all of them).
    expired: also every source that found streams, none of which is usable any more. failed: also every source whose run failed.
    Returns (runs to refresh, names that are not a source of this task)."""
    by_label: dict[str, list[dict]] = {}
    for r in runs:
        by_label.setdefault(r["label"].strip().lower(), []).append(r)
    chosen: dict[int, dict] = {}
    unknown: list[str] = []
    for n in names:
        hit = by_label.get(n.strip().lower())
        if not hit:
            unknown.append(n)
            continue
        for r in hit:
            chosen[r["id"]] = r
    for r in runs:
        if failed and r["status"] == "error":
            chosen[r["id"]] = r
        if expired and r["status"] == "ok" and r["streams"] and not any(stream_alive(e, dead, now) for e, dead in r["streams"]):
            chosen[r["id"]] = r
    return list(chosen.values()), unknown
