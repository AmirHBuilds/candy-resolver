"""Log records -> Telegram (the logbuf hook), with a fake Telegram. Run: python -m tests.smoke_notify_hooks"""
import asyncio
import logging
import tempfile
from pathlib import Path

from app import logbuf, notify
from app.notify import Notifier

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


async def main():
    sent = []

    async def fake(api_base, token, method, payload):
        sent.append(payload)
        return {"ok": True}

    n = Notifier(path=Path(tempfile.mkdtemp()) / "t.json", sender=fake, pace=0, defaults=lambda: {})
    await n.start()
    n.update({"enabled": True, "bot_token": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123", "chat_id": "-100",
              "categories": {"server_errors": {"topic_id": 3}, "warnings": {"enabled": True, "topic_id": 4}, "admin_events": {"topic_id": 5}}})
    notify.notifier.__dict__.update(n.__dict__)              # the hooks talk to the module-level notifier

    async def settle():
        await asyncio.sleep(0.05); await n._queue.join()

    lg = logging.getLogger("candy.test"); lg.propagate = False; lg.setLevel(logging.DEBUG)
    lg.addHandler(logbuf._Handler(level=logging.INFO))
    lg.info("just info"); await settle()
    check("INFO records are not sent", not sent)
    lg.error("something broke: %s", "boom"); await settle()
    check("ERROR goes to the server-errors topic", len(sent) == 1 and sent[0]["message_thread_id"] == 3 and "something broke: boom" in sent[0]["text"], str(sent[:1]))
    lg.warning("careful"); await settle()
    check("WARNING goes to the warnings topic (when switched on)", len(sent) == 2 and sent[1]["message_thread_id"] == 4)
    try:
        raise ValueError("deep cause")
    except ValueError:
        lg.exception("crashed")
    await settle()
    check("exceptions include the traceback", "ValueError: deep cause" in sent[-1]["text"] and "Traceback" in sent[-1]["text"])
    k = len(sent)
    lg.error("already reported elsewhere", extra={"tg_skip": True}); await settle()
    check("records marked tg_skip are not sent twice", len(sent) == k)
    for name in ("candyresolver.notify", "httpx", "uvicorn.access"):
        l2 = logging.getLogger(name); l2.propagate = False; l2.addHandler(logbuf._Handler(level=logging.INFO)); l2.error("noise from " + name)
    await settle()
    check("the notifier's own and noisy loggers are never forwarded", len(sent) == k)
    for _ in range(4):
        lg.error("same again"); await settle()
    check("a repeated error is sent once", sum(1 for s in sent if "same again" in s["text"]) == 1)

    logbuf.add("WARNING", "admin", "disk clean: removed 8 item(s), freed 1000 bytes"); await settle()
    check("admin actions (WARNING from 'admin') go to their topic", sent[-1]["message_thread_id"] == 5 and "disk clean" in sent[-1]["text"])
    k = len(sent)
    logbuf.add("INFO", "admin", "deleted lib_x from disk (panel)"); await settle()
    check("routine admin INFO lines are not sent", len(sent) == k)
    logbuf.add("ERROR", "candy.test", "bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 leaked")
    check("bot tokens are masked in the panel's log buffer", "ABCDEFGH" not in logbuf._buf[-1]["msg"], logbuf._buf[-1]["msg"])
    print("ALL OK" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
