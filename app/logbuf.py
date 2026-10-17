"""In-memory ring buffer of recent log lines, for the admin panel's live log view.

It attaches a logging handler to the root logger and to uvicorn's loggers, and (optionally) tees stdout/stderr so
plain print() output from the app shows up too. Nothing here is persisted: `docker compose logs api` stays the
permanent record; this is the "what just happened" view.
"""
import itertools
import logging
import re
import sys
import threading
import time
from collections import deque

MAX_LINES = 3000
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

# Lines that would drown everything else (health checks every 10 s, the panel polling itself).
_NOISE = ("GET /health", "/admin/system", "/admin/logs", "/admin/processes", "/admin/overview")

_lock = threading.Lock()
_buf: deque = deque(maxlen=MAX_LINES)
_ids = itertools.count(1)
_installed = False


# Never show secrets in the panel: admin token, signed-link signatures, URL tokens, API keys.
_SECRETS = re.compile(r"(ADMIN_TOKEN[:=]\s*|X-Admin-Token[:=]\s*|X-API-Key[:=]\s*|(?:sig|token|key|password)=)[^\s&'\"]+", re.I)
_APIKEY = re.compile(r"\bcr_[A-Za-z0-9_-]{20,}|\bbot\d{5,}:[A-Za-z0-9_-]{20,}")   # also Telegram bot tokens inside API urls


def add(level: str, logger: str, msg: str, ts: float | None = None) -> None:
    msg = (msg or "").rstrip()
    if not msg or any(n in msg for n in _NOISE):
        return
    msg = _APIKEY.sub("cr_***", _SECRETS.sub(lambda m: m.group(1) + "***", msg))
    with _lock:
        _buf.append({"id": next(_ids), "ts": ts or time.time(), "level": level, "logger": logger, "msg": msg[:4000]})
    if logger == "admin" and level in ("WARNING", "ERROR", "CRITICAL"):       # things done from the admin panel
        try:
            from .notify import notifier
            notifier.emit("admin_events", "Admin action", {}, msg, level="warning", key=f"admin:{msg[:80]}", dedupe_s=30)
        except Exception:
            pass


# Loggers whose records must never be forwarded to Telegram (the sender's own, and noisy libraries).
_NO_FORWARD = ("candyresolver.notify", "httpx", "httpcore", "uvicorn.access")


def _forward(record: logging.LogRecord, msg: str) -> None:
    """WARNING / ERROR log records -> Telegram (server_errors / warnings topics). Records marked tg_skip are already
    reported in a better place (e.g. a failed download)."""
    if record.levelno < logging.WARNING or getattr(record, "tg_skip", False) or record.name.startswith(_NO_FORWARD):
        return
    from .notify import notifier
    error = record.levelno >= logging.ERROR
    notifier.emit("server_errors" if error else "warnings", f"{record.levelname}: {record.name}", {}, msg[-1800:],
                  level="critical" if record.levelno >= logging.CRITICAL else "error" if error else "warning",
                  key=f"log:{record.name}:{record.levelno}:{msg[:100]}")


class _Handler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + (self.formatter or logging.Formatter()).formatException(record.exc_info)
            add(record.levelname, record.name, msg, record.created)
            _forward(record, msg)
        except Exception:  # a logging handler must never raise
            pass


class _Tee:
    """Wraps sys.stdout / sys.stderr: passes everything through and also records complete lines."""

    def __init__(self, stream, level: str, name: str):
        self._s, self._level, self._name, self._part = stream, level, name, ""

    def write(self, data):
        n = self._s.write(data)
        try:
            self._part += data
            *lines, self._part = self._part.split("\n")
            for ln in lines:
                add(self._level, self._name, ln)
            self._part = self._part[-4000:]
        except Exception:
            pass
        return n

    def __getattr__(self, name):
        return getattr(self._s, name)


def install() -> None:
    """Idempotent. Call once at startup."""
    global _installed
    if _installed:
        return
    _installed = True
    h = _Handler(level=logging.INFO)
    logging.getLogger().addHandler(h)
    if logging.getLogger().level in (logging.NOTSET, logging.WARNING):
        logging.getLogger().setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.addHandler(h)
        lg.propagate = False  # we already attached the handler; avoid duplicates via the root logger
    sys.stdout = _Tee(sys.stdout, "INFO", "stdout")
    sys.stderr = _Tee(sys.stderr, "ERROR", "stderr")
    add("INFO", "candyresolver", "log capture started")


def query(min_level: str = "DEBUG", q: str = "", since_id: int = 0, limit: int = 300) -> dict:
    floor = LEVELS.get(min_level.upper(), 0)
    needle = q.lower().strip()
    with _lock:
        rows = list(_buf)
    last_id = rows[-1]["id"] if rows else 0
    out = [r for r in rows
           if r["id"] > since_id and LEVELS.get(r["level"], 20) >= floor and (not needle or needle in r["msg"].lower()
                                                                             or needle in r["logger"].lower())]
    return {"last_id": last_id, "total": len(rows), "lines": out[-max(1, min(limit, 2000)):]}


def count_since(ts: float, levels=("ERROR", "CRITICAL")) -> int:
    """How many log lines of these levels are in the buffer since `ts` (a recent window only: the buffer is limited)."""
    with _lock:
        return sum(1 for e in _buf if e["ts"] >= ts and e["level"] in levels)


def counts() -> dict:
    with _lock:
        rows = list(_buf)
    c = {"errors": 0, "warnings": 0}
    cutoff = time.time() - 3600
    for r in rows:
        if r["ts"] < cutoff:
            continue
        if r["level"] in ("ERROR", "CRITICAL"):
            c["errors"] += 1
        elif r["level"] == "WARNING":
            c["warnings"] += 1
    return c


def clear() -> None:
    with _lock:
        _buf.clear()
