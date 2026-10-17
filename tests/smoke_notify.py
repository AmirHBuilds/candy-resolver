"""Telegram notifier with a FAKE Telegram (no network). Run: python -m tests.smoke_notify"""
import asyncio
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

from app.notify import Notifier, explain, scrub

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


class Fake:
    """Pretends to be Telegram. `script` = list of answers to give next (then ok)."""
    def __init__(self):
        self.calls, self.script = [], []

    async def __call__(self, api_base, token, method, payload):
        self.calls.append((api_base, token, method, dict(payload)))
        if self.script:
            a = self.script.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        return {"ok": True, "result": {}}

    @property
    def sends(self):
        return [c for c in self.calls if c[2] == "sendMessage"]


class Clock:
    t = 1000.0
    def __call__(self): return self.t


async def make(fake, **kw):
    d = Path(tempfile.mkdtemp())
    clk = Clock()
    async def nosleep(_): await asyncio.sleep(0)
    n = Notifier(path=d / "telegram.json", sender=fake, pace=0, clock=clk, sleep=nosleep, defaults=lambda: {})
    await n.start()
    n.update({"enabled": True, "bot_token": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123", "chat_id": "-1001234567890", **kw})
    return n, clk, d


async def settle(n):
    await asyncio.sleep(0.05)
    await n._queue.join()


async def main():
    # ---- settings file ----
    fake = Fake()
    n, clk, d = await make(fake, server_name="vps-1")
    mode = stat.S_IMODE(os.stat(d / "telegram.json").st_mode)
    check("settings file is private (600)", mode == 0o600, oct(mode))
    pub = n.public()
    check("token is never returned, only a hint", "ABCDEFG" not in json.dumps(pub) and pub["bot_token_set"] and pub["bot_token_hint"] == "••••0123")
    n.update({"bot_token": None, "chat_id": "-100999"})
    check("token None keeps it, other fields change", n.cfg["bot_token"].endswith("0123") and n.cfg["chat_id"] == "-100999")
    n.update({"chat_id": "-1001234567890", "categories": {"nope": {"enabled": True}, "system": {"topic_id": 77}}})
    check("unknown category ignored, known one updated", "nope" not in n.cfg["categories"] and n.cfg["categories"]["system"]["topic_id"] == 77)
    n2 = Notifier(path=d / "telegram.json", defaults=lambda: {}); n2.load()
    check("settings survive a restart", n2.cfg["chat_id"] == "-1001234567890" and n2.cfg["categories"]["system"]["topic_id"] == 77 and n2.cfg["server_name"] == "vps-1")
    n3 = Notifier(path=d / "none.json", defaults=lambda: {"bot_token": "1:" + "a" * 25, "chat_id": "-5", "server_name": "x"}); n3.load()
    check("first start takes the environment defaults and enables itself", n3.cfg["enabled"] and n3.cfg["chat_id"] == "-5")
    n.update({"bot_token": ""})
    check("token '' clears it and nothing is sent", not n.ready("system"))
    n.update({"bot_token": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"})

    # ---- routing: topic + chat override ----
    n.update({"categories": {"source_errors": {"topic_id": 11}, "download_errors": {"topic_id": 22}, "system": {"topic_id": None},
                             "server_errors": {"chat_id": "-100777", "topic_id": 33}}})
    n.emit("source_errors", "Source failed: Vertex", {"Task": "tsk_1", "Title": "Obsession <b>"}, "Traceback...\nValueError: boom")
    n.emit("download_errors", "Download failed", {"Item": "lib_1"}, "HTTP 401")
    n.emit("system", "Server started")
    n.emit("server_errors", "ERROR: x", {}, "kaboom")
    await settle(n)
    by = {s[3].get("message_thread_id"): s[3] for s in fake.sends}
    check("each category goes to its own topic", set(by) == {11, 22, None, 33}, str(set(by)))
    check("no topic set -> no message_thread_id (general chat)", "message_thread_id" not in [s[3] for s in fake.sends if "Server started" in s[3]["text"]][0])
    check("per-category chat id override", [s[3]["chat_id"] for s in fake.sends if s[3].get("message_thread_id") == 33] == ["-100777"])
    t = by[11]["text"]
    check("HTML escaped, title bold, detail in <pre>", "Obsession &lt;b&gt;" in t and "<b>Source failed: Vertex</b>" in t and "<pre>Traceback...\nValueError: boom</pre>" in t)
    check("footer has server name", "vps-1" in t and "UTC" in t)
    check("token goes to the API call, not into the text", all("ABCDEFG" not in s[3]["text"] for s in fake.sends) and fake.sends[0][1].startswith("123456:"))

    # ---- scrubbing ----
    fake.calls.clear()
    n.emit("server_errors", "leak", {"url": "https://x/a?token=SECRETVALUE&b=1"}, "GET https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123/sendMessage X-API-Key: cr_" + "k" * 30, key="leak")
    await settle(n)
    txt = fake.sends[0][3]["text"]
    check("secrets are scrubbed from messages", "SECRETVALUE" not in txt and "ABCDEFGHIJ" not in txt and "cr_kkk" not in txt, txt[:200])

    # ---- dedupe ----
    fake.calls.clear()
    for _ in range(5):
        n.emit("source_errors", "Source failed: A", {}, "same error", key="A")
    await settle(n)
    check("same error 5x -> one message", len(fake.sends) == 1 and n.stats["suppressed"] >= 4)
    clk.t += 601
    n.emit("source_errors", "Source failed: A", {}, "same error", key="A")
    await settle(n)
    check("after the window the next one says how many were held back", len(fake.sends) == 2 and "+4 more" in fake.sends[1][3]["text"], fake.sends[-1][3]["text"][:120])
    n.emit("source_errors", "Source failed: B", {}, "other", key="B")
    await settle(n)
    check("a different key is not suppressed", len(fake.sends) == 3)

    # ---- disabled category / master switch ----
    fake.calls.clear()
    n.update({"categories": {"warnings": {"enabled": False}}})
    n.emit("warnings", "w", key="w1"); await settle(n)
    check("disabled category sends nothing (warnings are off by default)", not fake.sends)
    n.update({"enabled": False}); n.emit("system", "s", key="s9"); await settle(n)
    check("master switch off sends nothing", not fake.sends)
    n.update({"enabled": True})

    # ---- rate limit ----
    clk.t += 120; fake.calls.clear(); n.update({"rate_per_min": 3})
    for i in range(8):
        n.emit("system", f"burst {i}", key=f"b{i}")
    await settle(n)
    check("per-minute cap: only 3 sent", len(fake.sends) == 3, str(len(fake.sends)))
    clk.t += 61
    n.emit("system", "after", key="after"); await settle(n)
    check("next message mentions the dropped ones", "dropped" in fake.sends[-1][3]["text"], fake.sends[-1][3]["text"][:140])
    n.update({"rate_per_min": 20})

    # ---- retries ----
    fake.calls.clear(); fake.script = [{"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 2}}]
    n.emit("system", "retry me", key="r1"); await settle(n)
    check("429 retry_after -> sent again", len(fake.sends) == 2 and n.stats["sent"] >= 1)
    fake.calls.clear(); fake.script = [OSError("connection reset"), {"ok": True}]
    n.emit("system", "net", key="r2"); await settle(n)
    check("network error -> retried and delivered", len(fake.sends) == 2)
    fake.calls.clear(); fake.script = [{"ok": False, "description": "Bad Request: can't parse entities: bad"}]
    n.emit("system", "fmt <x>", key="r3"); await settle(n)
    check("formatting error -> resent as plain text", len(fake.sends) == 2 and "parse_mode" not in fake.sends[1][3] and "<b>" not in fake.sends[1][3]["text"])
    before = n.stats["failed"]
    fake.calls.clear(); fake.script = [{"ok": False, "description": "Bad Request: message thread not found"}]
    n.emit("system", "bad topic", key="r4"); await settle(n)
    le = n.stats["last_error"]
    check("permanent error: not retried, recorded with a hint", len(fake.sends) == 1 and n.stats["failed"] == before + 1 and "topic" in le["hint"].lower(), str(le))

    # ---- long message ----
    fake.calls.clear()
    n.emit("server_errors", "long", {"a": "b"}, "x" * 3000 + "THE_CAUSE", key="long"); await settle(n)
    tl = fake.sends[0][3]["text"]
    check("long detail is cut to fit, the END (cause) is kept", len(tl) <= 4096 and "THE_CAUSE" in tl)

    # ---- thread safety ----
    fake.calls.clear()
    th = threading.Thread(target=lambda: n.emit("system", "from a thread", key="thr")); th.start(); th.join()
    await settle(n)
    check("emit works from another thread", len(fake.sends) == 1)

    # ---- not started / never raises ----
    cold = Notifier(path=d / "cold.json", defaults=lambda: {})
    cold.emit("system", "nobody listens")
    check("emit before start is a silent no-op", True)
    n.emit("not_a_category", "x"); n.emit("system", None); n.emit("system", "x", fields={"a": object()}, key="obj")
    await settle(n)
    check("bad input never raises", True)

    # ---- test message + helpers ----
    fake.calls.clear()
    r = await n.send_test("download_errors")
    check("send_test goes to that topic and reports ok", r["ok"] and r["topic_id"] == 22 and fake.sends[0][3]["message_thread_id"] == 22)
    fake.calls.clear(); fake.script = [{"ok": False, "description": "Forbidden: bot was kicked from the supergroup chat"}]
    r = await n.send_test(None)
    check("send_test explains failures", not r["ok"] and "removed" in r["hint"], r["hint"])
    check("explain() known + unknown", "token" in explain("Unauthorized") and explain("???") == "")
    check("scrub() masks bot tokens in urls", "ABCDEF" not in scrub("POST https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123/x"))

    # ---- finding chat / topic ids ----
    fake.calls.clear()
    fake.script = [{"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": -1001, "title": "Ops", "type": "supergroup"}, "message_thread_id": 5, "is_topic_message": True,
                                      "reply_to_message": {"forum_topic_created": {"name": "Errors"}}, "text": "/ping"}},
        {"update_id": 2, "message": {"chat": {"id": -1001, "title": "Ops", "type": "supergroup"}, "message_thread_id": 5, "is_topic_message": True, "text": "hello"}},
        {"update_id": 3, "message": {"chat": {"id": -1001, "title": "Ops", "type": "supergroup"}, "text": "in general"}},
        {"update_id": 4, "message": {"chat": {"id": 42, "first_name": "Amir", "type": "private"}, "text": "hi"}}]}]
    u = await n.updates()
    items = {(i["chat_id"], i["topic_id"]): i for i in u["items"]}
    check("updates: topics and chats found, topic name known, duplicates merged", u["ok"] and len(items) == 3 and items[(-1001, 5)]["topic"] == "Errors" and items[(-1001, None)]["last"] == "in general" and items[(42, None)]["type"] == "private", str(items))
    fake.script = [{"ok": False, "description": "Conflict: can't use getUpdates method while webhook is active"}]
    u = await n.updates()
    check("updates: error is passed on", not u["ok"] and "webhook" in u["error"])

    # ---- shutdown flushes the queue ----
    fake.calls.clear()
    n.emit("system", "last words", key="lw")
    await n.stop(flush_s=2)
    check("stop() delivers what is queued", len(fake.sends) == 1)
    check("after stop emit is a no-op", (n.emit("system", "late", key="late") or True) and len(fake.sends) == 1)

    print("ALL OK" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
