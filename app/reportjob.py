"""Collects the numbers for the Telegram daily report (from the database) and sends it, on schedule or on demand."""
import asyncio
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from . import autoclean, logbuf
from .db import SessionLocal
from .models import LibraryItem, SourceRun, Task
from .notify import explain, notifier
from .report import format_report, report_due
from .version import VERSION

log = logging.getLogger("candyresolver.notify")        # (ignored by the log -> Telegram hook)


def _reason(error: str | None) -> str:
    e = (error or "unknown").strip()
    if e.startswith("source_link_expired"):
        m = re.match(r"source_link_expired \(([^)]*)\)", e)
        return "source link expired" + (f" ({m.group(1)})" if m else "")
    return re.sub(r"https?://\S+|\d+", "#", e.splitlines()[0])[:70]


async def collect(period_h: int = 24) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=period_h)
    async with SessionLocal() as db:
        by_status = dict((await db.execute(select(Task.status, func.count()).where(Task.created_at >= since).group_by(Task.status))).all())
        runs = (await db.execute(select(SourceRun.source_name, SourceRun.status, SourceRun.duration_ms)
                                 .join(Task, Task.id == SourceRun.task_id).where(Task.created_at >= since))).all()
        items = (await db.execute(select(LibraryItem.status, LibraryItem.size, LibraryItem.error, LibraryItem.ready_at)
                                  .where(LibraryItem.requested_at >= since, LibraryItem.mode != "subs"))).all()
        lib_items, lib_bytes = (await db.execute(select(func.count(), func.coalesce(func.sum(LibraryItem.size), 0))
                                                 .where(LibraryItem.status == "ready"))).one()
    per: dict[str, dict] = {}
    for name, status, ms in runs:
        s = per.setdefault(name, {"name": name, "runs": 0, "ok": 0, "empty": 0, "error": 0, "ms": []})
        s["runs"] += 1
        s[status if status in ("ok", "empty") else "error"] += 1
        if status != "error" and ms is not None:
            s["ms"].append(ms)
    sources = [{**{k: v for k, v in s.items() if k != "ms"}, "avg_ms": int(sum(s["ms"]) / len(s["ms"])) if s["ms"] else None} for s in per.values()]
    finished = [i for i in items if i[3] is not None]
    failed = [i for i in items if i[0] == "failed"]
    system = {}
    try:
        from . import sysmon
        snap = sysmon.snapshot()
        system = {"uptime_s": snap["uptime_s"], "load": (snap["host"]["load"] or [None])[0], "mem_percent": snap["memory"]["percent"],
                  "disk_free": snap["disk"]["free"], "disk_total": snap["disk"]["total"]}
    except Exception:
        log.debug("could not read system numbers for the report", exc_info=True)
    return {
        "period_h": period_h, "version": VERSION, "errors_logged": logbuf.count_since(since.timestamp()),
        "tasks": {"total": sum(by_status.values()), "done": by_status.get("done", 0), "failed": by_status.get("failed", 0)},
        "sources": sources,
        "downloads": {"ready": len(finished), "ready_bytes": sum(i[1] or 0 for i in finished), "failed": len(failed),
                      "other": sum(1 for i in items if i[0] in ("queued", "downloading")),
                      "reasons": Counter(_reason(i[2]) for i in failed).most_common(3)},
        "library": {"items": int(lib_items or 0), "bytes": int(lib_bytes or 0)},
        "system": system, "autoclean": autoclean.state.cfg.get("last"),
    }


async def send(trigger: str = "manual") -> dict:
    """Build the report and send it to the Daily report topic. Returns Telegram's answer (ok / error / hint)."""
    chat, topic = notifier.dest("reports")
    if not (notifier.cfg["bot_token"] and chat):
        return {"ok": False, "error": "Telegram is not set up (token and chat id).", "hint": ""}
    text = format_report(await collect(), notifier.server(), datetime.now(timezone.utc))
    r = await notifier.send_text(chat, topic, text)
    d = None if r.get("ok") else (r.get("description") or "unknown error")
    return {"ok": bool(r.get("ok")), "error": d, "hint": explain(d), "trigger": trigger}


async def loop(tick: float = 30) -> None:
    """Send the daily report once per day at the chosen time. A failed send is retried every 10 minutes."""
    retry_at = 0.0
    while True:
        try:
            cfg = notifier.cfg
            rep = cfg["report"]
            if cfg["enabled"] and rep["enabled"] and notifier.ready("reports") and time.time() >= retry_at:
                due, today = report_due(datetime.now(timezone.utc), rep["time"], float(rep["utc_offset"]), rep["last_sent"])
                if due:
                    r = await send("scheduled")
                    if r["ok"]:
                        notifier.mark_report_sent(today)
                    else:
                        retry_at = time.time() + 600
                        log.debug("daily report not sent: %s", r["error"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("daily report failed", exc_info=True)
        await asyncio.sleep(tick)
