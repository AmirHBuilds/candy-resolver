"""Telegram notifications: important errors and events go to a Telegram group, one TOPIC per kind of message.

emit() never blocks and never raises: it filters, de-duplicates and queues; one background worker sends (about 1 message
per second, with a per-minute cap and retries), so a burst of errors can never slow the server down or flood the group.
Settings live in DATA_DIR/telegram.json (mode 600, the bot token is never returned by the API) and are edited in the admin panel."""
import asyncio
import html
import json
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("candyresolver.notify")     # the log->Telegram hook ignores this logger (no feedback loops)

# key -> (label, description, on by default, icon)
CATEGORIES = {
    "source_errors": ("Source failures", "A source script failed, or a whole task found nothing", True, "🧩"),
    "download_errors": ("Download failures", "A library download failed (expired link, disk full, ...)", True, "⬇️"),
    "server_errors": ("Server errors", "Python errors and exceptions inside the server (log level ERROR)", True, "🛑"),
    "warnings": ("Warnings", "Log level WARNING: can be chatty", False, "⚠️"),
    "admin_events": ("Admin actions", "Cleanups, deletes and process kills done from the panel", True, "🛠"),
    "system": ("System", "Server start / stop, low disk space, automatic cleanups", True, "🖥"),
    "reports": ("Daily report", "A summary of the last 24 hours (tasks, sources, downloads, disk, server)", True, "📊"),
}
LEVEL_ICON = {"critical": "🚨", "error": "🔴", "warning": "🟠", "info": "🟢"}
API_BASE = "https://api.telegram.org"
TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
_BOT_IN_URL = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]+")
MAX_QUEUE = 200


def scrub(text: str) -> str:
    """No tokens, signed-link secrets or API keys in a chat message."""
    from . import logbuf
    text = logbuf._APIKEY.sub("cr_***", logbuf._SECRETS.sub(lambda m: m.group(1) + "***", text or ""))
    return _BOT_IN_URL.sub("bot***", text)


def explain(description: str | None) -> str:
    """Telegram's error text -> what to do about it."""
    d = (description or "").lower()
    for needle, hint in (
        ("unauthorized", "The bot token is wrong."),
        ("message thread not found", "Wrong topic id (use the topic's number, not its name), or that topic was deleted."),
        ("chat not found", "Wrong chat id, or the bot has not been added to that group."),
        ("not enough rights", "The bot may not post here: make it an admin of the group / allow it to write in the topic."),
        ("bot was kicked", "The bot was removed from the group: add it again."),
        ("bot is not a member", "Add the bot to the group first."),
        ("topic_closed", "That topic is closed: reopen it."),
        ("forbidden", "The bot is not allowed to post there: add it to the group (as admin)."),
        ("too many requests", "Telegram rate limit: it will be retried."),
        ("can't parse entities", "A message could not be formatted."),
    ):
        if needle in d:
            return hint
    return ""


async def http_sender(api_base: str, token: str, method: str, payload: dict, timeout: float = 15) -> dict:
    """Default transport: the Telegram Bot API over HTTPS. Returns Telegram's JSON answer."""
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{api_base.rstrip('/')}/bot{token}/{method}", json=payload)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "description": f"HTTP {r.status_code}"}


def _default_path() -> Path:
    from .config import settings
    return Path(settings.data_dir) / "telegram.json"


def _env_defaults() -> dict:
    from .config import settings
    return {"bot_token": settings.telegram_bot_token, "chat_id": settings.telegram_chat_id, "server_name": settings.server_name}


class Notifier:
    def __init__(self, path=None, sender=None, defaults=None, pace: float = 1.05, clock=time.time, sleep=asyncio.sleep):
        self._path_arg, self._defaults_arg = path, defaults
        self._sender, self._pace, self._clock, self._sleep = sender or http_sender, pace, clock, sleep
        self._lock = threading.Lock()
        self._loop = None
        self._queue: asyncio.Queue | None = None
        self._task = None
        self._seen: dict[str, list] = {}
        self._sent_at: list[float] = []
        self._dropped_note = 0
        self.stats = {"sent": 0, "failed": 0, "suppressed": 0, "dropped": 0, "last_error": None, "last_ok_at": None}
        self.cfg = self._blank()

    # ---------------- settings ----------------
    @staticmethod
    def _blank() -> dict:
        return {"enabled": False, "bot_token": "", "chat_id": "", "server_name": "", "api_base": API_BASE,
                "dedupe_s": 600, "rate_per_min": 20,
                "low_disk_gb": 2.0, "low_disk_repeat_h": 6, "fail_streak": 5,
                "report": {"enabled": True, "time": "08:00", "utc_offset": 0.0, "last_sent": None},
                "categories": {k: {"enabled": v[2], "topic_id": None, "chat_id": ""} for k, v in CATEGORIES.items()}}

    @property
    def path(self) -> Path:
        return Path(self._path_arg) if self._path_arg else _default_path()

    def load(self) -> None:
        cfg = self._blank()
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = None
        if stored is None:                                   # first start: environment variables give the initial values
            try:
                env = (self._defaults_arg or _env_defaults)()
            except Exception:
                env = {}
            for k in ("bot_token", "chat_id", "server_name"):
                cfg[k] = (env.get(k) or "").strip()
            cfg["enabled"] = bool(cfg["bot_token"] and cfg["chat_id"])
        else:
            for k, v in stored.items():
                if k == "categories" and isinstance(v, dict):
                    for ck, cv in v.items():
                        if ck in cfg["categories"] and isinstance(cv, dict):
                            cfg["categories"][ck].update({x: cv[x] for x in ("enabled", "topic_id", "chat_id") if x in cv})
                elif k == "report" and isinstance(v, dict):
                    cfg["report"].update({x: v[x] for x in ("enabled", "time", "utc_offset", "last_sent") if x in v})
                elif k in cfg:
                    cfg[k] = v
        with self._lock:
            self.cfg = cfg

    def save(self) -> None:
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".telegram-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, patch: dict) -> None:
        """patch: only the keys to change. bot_token None = keep, '' = remove. Saved to disk."""
        with self._lock:
            c = self.cfg
            for k in ("enabled", "chat_id", "server_name", "api_base", "dedupe_s", "rate_per_min", "low_disk_gb", "low_disk_repeat_h", "fail_streak"):
                if patch.get(k) is not None:
                    c[k] = patch[k]
            for k, v in (patch.get("report") or {}).items():
                if k in ("enabled", "time", "utc_offset") and v is not None:
                    c["report"][k] = v
            if patch.get("bot_token") is not None:
                c["bot_token"] = patch["bot_token"].strip()
            for ck, cv in (patch.get("categories") or {}).items():
                if ck in c["categories"]:
                    c["categories"][ck].update({x: cv[x] for x in ("enabled", "topic_id", "chat_id") if x in cv})
        self.save()

    def public(self) -> dict:
        c = self.cfg
        return {"enabled": c["enabled"], "bot_token_set": bool(c["bot_token"]), "bot_token_hint": ("••••" + c["bot_token"][-4:]) if c["bot_token"] else "",
                "chat_id": c["chat_id"], "server_name": c["server_name"], "api_base": c["api_base"],
                "dedupe_s": c["dedupe_s"], "rate_per_min": c["rate_per_min"],
                "low_disk_gb": c["low_disk_gb"], "low_disk_repeat_h": c["low_disk_repeat_h"], "fail_streak": c["fail_streak"], "report": dict(c["report"]),
                "categories": [{"key": k, "label": CATEGORIES[k][0], "description": CATEGORIES[k][1], **v} for k, v in c["categories"].items()],
                "stats": dict(self.stats), "running": self._loop is not None}

    def mark_report_sent(self, day: str) -> None:
        with self._lock:
            self.cfg["report"]["last_sent"] = day
        try:
            self.save()
        except Exception:
            log.debug("could not save last_sent", exc_info=True)

    def server(self) -> str:
        return self.cfg["server_name"] or socket.gethostname()

    def dest(self, category: str) -> tuple[str, int | None]:
        cat = self.cfg["categories"].get(category) or {}
        return (cat.get("chat_id") or self.cfg["chat_id"] or "").strip(), (cat.get("topic_id") or None)

    def ready(self, category: str) -> bool:
        c = self.cfg
        cat = c["categories"].get(category)
        return bool(c["enabled"] and c["bot_token"] and cat and cat.get("enabled") and self.dest(category)[0])

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self.load()
        for name in ("httpx", "httpcore"):                   # their INFO lines contain the bot token in the URL
            logging.getLogger(name).setLevel(logging.WARNING)
        self._task = asyncio.create_task(self._worker())

    async def stop(self, flush_s: float = 3) -> None:
        if self._queue is not None:
            await asyncio.sleep(0.05)                         # let emit() calls made just before stop() reach the queue
            try:
                await asyncio.wait_for(self._queue.join(), flush_s)
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
        self._loop = None

    # ---------------- emitting (safe from any thread) ----------------
    def emit(self, category: str, title: str, fields: dict | None = None, detail: str = "", level: str = "error",
             key: str | None = None, dedupe_s: int | None = None) -> None:
        try:
            if self._loop is None or not self.ready(category):
                return
            now = self._clock()
            window = self.cfg["dedupe_s"] if dedupe_s is None else dedupe_s
            k = key or f"{category}:{title}"
            with self._lock:
                ent = self._seen.get(k)
                if ent and window > 0 and now - ent[0] < window:
                    ent[1] += 1
                    self.stats["suppressed"] += 1
                    return
                more = ent[1] if ent else 0
                self._seen[k] = [now, 0]
                if len(self._seen) > 2000:                    # forget the oldest keys
                    for old in sorted(self._seen, key=lambda x: self._seen[x][0])[:500]:
                        del self._seen[old]
            item = {"category": category, "title": title, "fields": fields or {}, "detail": detail or "", "level": level, "more": more}
            self._loop.call_soon_threadsafe(self._enqueue, item)
        except Exception:
            pass

    def reset_key(self, key: str) -> None:
        """Forget that `key` was sent recently, so its next message goes out at once (a new incident, not a repeat)."""
        with self._lock:
            self._seen.pop(key, None)

    def _enqueue(self, item: dict) -> None:
        if self._queue is None:
            return
        if self._queue.qsize() >= MAX_QUEUE:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except Exception:
                pass
            self.stats["dropped"] += 1
            self._dropped_note += 1
        self._queue.put_nowait(item)

    # ---------------- formatting ----------------
    def format(self, item: dict) -> str:
        e = lambda s: html.escape(str(s), quote=False)
        icon = CATEGORIES.get(item["category"], ("", "", True, ""))[3]
        head = [f"{LEVEL_ICON.get(item['level'], 'ℹ️')} <b>{e(scrub(item['title']))}</b>"]
        for k, v in item["fields"].items():
            if v not in (None, ""):
                head.append(f"<b>{e(k)}:</b> {e(scrub(str(v)))}")
        if item.get("more"):
            head.append(f"<i>+{item['more']} more like this since the last message</i>")
        if self._dropped_note:
            head.append(f"<i>⚠️ {self._dropped_note} message(s) were dropped (too many at once) before this one</i>")
            self._dropped_note = 0
        foot = f"\n<i>{icon} {e(self.server())} · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</i>"
        text = "\n".join(head)
        detail = scrub(item["detail"]).strip()
        budget = 4000 - len(text) - len(foot) - 20
        if detail and budget > 60:
            while len(e(detail)) > budget:
                detail = detail[max(1, len(detail) // 10):]      # keep the END (tracebacks end with the cause)
            text += f"\n<pre>{e(detail)}</pre>"
        return (text + foot)[:4096]

    # ---------------- delivery ----------------
    async def _call(self, method: str, payload: dict) -> dict:
        try:
            return await self._sender(self.cfg["api_base"], self.cfg["bot_token"], method, payload)
        except Exception as ex:
            return {"ok": False, "network": True, "description": scrub(f"{ex.__class__.__name__}: {ex}")}

    async def send_text(self, chat_id: str, topic_id: int | None, text: str, attempts: int = 3) -> dict:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if topic_id:
            payload["message_thread_id"] = int(topic_id)
        r: dict = {}
        for i in range(attempts):
            r = await self._call("sendMessage", payload)
            if r.get("ok"):
                return r
            desc = (r.get("description") or "").lower()
            retry_after = (r.get("parameters") or {}).get("retry_after")
            if retry_after and i < attempts - 1:
                await self._sleep(min(int(retry_after), 30) + 0.5)
            elif r.get("network") and i < attempts - 1:
                await self._sleep(2 * (i + 1))
            elif "parse entities" in desc and "parse_mode" in payload:
                payload.pop("parse_mode")
                payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", payload["text"]))
            else:
                break
        return r

    async def _deliver(self, item: dict) -> None:
        now = self._clock()
        self._sent_at = [t for t in self._sent_at if now - t < 60]
        if len(self._sent_at) >= max(1, int(self.cfg["rate_per_min"])):
            self.stats["dropped"] += 1
            self._dropped_note += 1
            return
        chat, topic = self.dest(item["category"])
        if not chat:
            return
        self._sent_at.append(now)
        r = await self.send_text(chat, topic, self.format(item))
        if r.get("ok"):
            self.stats["sent"] += 1
            self.stats["last_ok_at"] = now
        else:
            self.stats["failed"] += 1
            d = r.get("description") or "unknown error"
            self.stats["last_error"] = {"at": now, "category": item["category"], "error": d, "hint": explain(d)}
            log.debug("telegram send failed: %s", d)

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("telegram worker error", exc_info=True)
            finally:
                self._queue.task_done()
            await self._sleep(self._pace)                     # Telegram allows about 1 message per second per chat

    # ---------------- admin helpers ----------------
    async def send_test(self, category: str | None) -> dict:
        c = self.cfg
        if not c["bot_token"]:
            return {"ok": False, "error": "Enter the bot token first (and save).", "hint": ""}
        key = category if category in CATEGORIES else "system"
        chat, topic = self.dest(key)
        if not chat:
            return {"ok": False, "error": "No chat id set (and save).", "hint": ""}
        label = CATEGORIES[key][0]
        text = (f"✅ <b>Test message</b>\n<b>Topic:</b> {html.escape(label, quote=False)}\n"
                f"<i>{html.escape(self.server(), quote=False)} · if you can read this in the right topic, it works.</i>")
        r = await self.send_text(chat, topic, text, attempts=2)
        d = None if r.get("ok") else (r.get("description") or "unknown error")
        return {"ok": bool(r.get("ok")), "error": d, "hint": explain(d), "chat_id": chat, "topic_id": topic}

    async def updates(self) -> dict:
        """Chats and topics the bot has recently seen (send any message in a topic first), to find their ids."""
        if not self.cfg["bot_token"]:
            return {"ok": False, "error": "Enter the bot token first (and save).", "items": []}
        r = await self._call("getUpdates", {"limit": 100, "timeout": 0, "allowed_updates": ["message", "channel_post"]})
        if not r.get("ok"):
            d = r.get("description") or "unknown error"
            return {"ok": False, "error": d, "hint": explain(d), "items": []}
        seen: dict[tuple, dict] = {}
        for u in r.get("result", []):
            m = u.get("message") or u.get("channel_post")
            if not m or not m.get("chat"):
                continue
            ch = m["chat"]
            thread = m.get("message_thread_id") if (m.get("is_topic_message") or m.get("message_thread_id")) else None
            name = ((m.get("forum_topic_created") or {}).get("name")
                    or ((m.get("reply_to_message") or {}).get("forum_topic_created") or {}).get("name"))
            ent = seen.setdefault((ch["id"], thread), {"chat_id": ch["id"], "chat": ch.get("title") or ch.get("username") or ch.get("first_name") or "",
                                                       "type": ch.get("type"), "topic_id": thread, "topic": None, "last": ""})
            ent["topic"] = ent["topic"] or name
            ent["last"] = (m.get("text") or m.get("caption") or "")[:60]
        return {"ok": True, "items": list(seen.values())}


notifier = Notifier()


def disk_state(free: int, threshold_gb: float, flagged: bool) -> str | None:
    """'low' when free space is under the limit; 'recovered' once it is clearly above it again (20 % margin, so it does not flap); else None.
    threshold_gb <= 0 means the warning is off."""
    if not threshold_gb or threshold_gb <= 0:
        return None
    limit = threshold_gb * 1e9
    if free < limit:
        return "low"
    if flagged and free >= limit * 1.2:
        return "recovered"
    return None


async def watch_disk(path, interval: float = 300) -> None:
    """Warn when the disk that holds the library has less free space than the limit set in the Telegram page (default 2 GB),
    again every `low_disk_repeat_h` hours while it stays low, and say when it is OK again."""
    flagged = False
    while True:
        try:
            d = shutil.disk_usage(path)
            st = disk_state(d.free, notifier.cfg.get("low_disk_gb", 2.0), flagged)
            gb = lambda n: f"{n / 1e9:.1f} GB"
            if st == "low":
                flagged = True
                notifier.emit("system", "Low disk space",
                              {"Free": f"{gb(d.free)} (your limit: {notifier.cfg.get('low_disk_gb', 2.0)} GB)", "Total": gb(d.total),
                               "Used": f"{100 * d.used / d.total:.0f}%",
                               "What to do": "Admin -> Files on disk -> Clean disk, or run scripts/host_cleanup.sh on the VPS"},
                              level="warning", key="lowdisk", dedupe_s=max(1, int(notifier.cfg.get("low_disk_repeat_h", 6))) * 3600)
            elif st == "recovered":
                flagged = False
                notifier.reset_key("lowdisk")                       # the next time it drops is a new problem: warn at once
                notifier.emit("system", "Disk space is OK again", {"Free": gb(d.free), "Total": gb(d.total)}, level="info", key="lowdisk-ok", dedupe_s=0)
            elif st is None and flagged and not notifier.cfg.get("low_disk_gb"):
                flagged = False                                  # the warning was switched off meanwhile
        except Exception:
            pass
        await asyncio.sleep(interval)
