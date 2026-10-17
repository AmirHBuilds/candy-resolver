"""Schedule of the automatic disk clean: its settings file, 'is it due?', and the loop that checks every minute.
Pure logic: the job itself (diskjob.automatic) is passed in, so this runs and tests without a database or web framework."""
import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger("candy.autoclean")
DEFAULTS = {"enabled": True, "every_hours": 6, "database": True, "emergency": True, "host": True, "host_aggressive": False, "last_run": None, "last": None}
EMERGENCY_GAP_S = 900              # when the disk is nearly full the clean may run again after 15 minutes instead of waiting for the schedule


def low_disk(free: int, total: int) -> bool:
    return total > 0 and free < max(1 << 30, total * 0.07)


class AutoClean:
    def __init__(self, path=None):
        self._path_arg = path
        self.cfg = dict(DEFAULTS)

    @property
    def path(self) -> Path:
        if self._path_arg:
            return Path(self._path_arg)
        from .config import settings
        return Path(settings.data_dir) / "autoclean.json"

    def load(self) -> None:
        cfg = dict(DEFAULTS)
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        self.cfg = cfg

    def save(self) -> None:
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".autoclean-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, patch: dict) -> None:
        for k in ("enabled", "every_hours", "database", "emergency", "host", "host_aggressive"):
            if patch.get(k) is not None:
                self.cfg[k] = patch[k]
        self.save()

    def due(self, now: float, free: int, total: int) -> str | None:
        """Why a clean should run now ('scheduled' / 'low disk'), or None."""
        c = self.cfg
        if not c["enabled"]:
            return None
        last = c["last_run"]
        if last is None or now - last >= c["every_hours"] * 3600:
            return "scheduled"
        if c["emergency"] and low_disk(free, total) and now - last >= EMERGENCY_GAP_S:
            return "low disk"
        return None

    def record(self, trigger: str, report: dict, now: float | None = None) -> None:
        now = now or time.time()
        self.cfg["last_run"] = now
        self.cfg["last"] = {"at": now, "trigger": trigger, "freed_bytes": report.get("freed_bytes", 0), "removed": report.get("removed", 0),
                            "errors": report.get("errors", 0), "database": report.get("database") or {}, "duration_s": report.get("duration_s")}
        try:
            self.save()
        except Exception:
            log.exception("could not save the automatic clean state")

    def public(self, now: float | None = None, disk: dict | None = None) -> dict:
        now = now or time.time()
        c = self.cfg
        nxt = None
        if c["enabled"]:
            nxt = 0 if c["last_run"] is None else max(0, int(c["last_run"] + c["every_hours"] * 3600 - now))
        return {"enabled": c["enabled"], "every_hours": c["every_hours"], "database": c["database"], "emergency": c["emergency"],
                "host": c["host"], "host_aggressive": c["host_aggressive"], "last": c["last"], "next_in_s": nxt, "disk": disk}


state = AutoClean()


async def run_loop(st: AutoClean, job, disk, first_delay: float = 600, tick: float = 60, clock=time.time, sleep=asyncio.sleep) -> None:
    """Every `tick` seconds: if a clean is due, run job(reason). disk() -> (free, total). Never dies on an error."""
    await sleep(first_delay)                       # let the server settle after a start
    while True:
        try:
            free, total = disk()
            reason = st.due(clock(), free, total)
            if reason:
                await job(reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("automatic cleanup failed")
        await sleep(tick)
