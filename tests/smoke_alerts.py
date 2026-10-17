"""Low-disk watcher and the new alert settings, with a fake disk and a fake Telegram. Run: python -m tests.smoke_alerts"""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from app import notify
from app.notify import Notifier

ok = True
GB = 10 ** 9


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


async def main():
    sent = []

    async def fake(api_base, token, method, payload):
        sent.append(payload)
        return {"ok": True}

    clock = {"t": 1000.0}
    n = Notifier(path=Path(tempfile.mkdtemp()) / "t.json", sender=fake, pace=0, clock=lambda: clock["t"], defaults=lambda: {})
    await n.start()
    check("defaults: warn below 2 GB, repeat 6 h, alert after 5 failed runs", (n.cfg["low_disk_gb"], n.cfg["low_disk_repeat_h"], n.cfg["fail_streak"]) == (2.0, 6, 5))
    check("daily report on by default at 08:00, with its own topic kind", n.cfg["report"]["enabled"] and n.cfg["report"]["time"] == "08:00" and "reports" in n.cfg["categories"])
    n.update({"enabled": True, "bot_token": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123", "chat_id": "-100", "low_disk_gb": 5, "low_disk_repeat_h": 2, "fail_streak": 0,
              "report": {"enabled": False, "time": "21:15", "utc_offset": 3.5}, "categories": {"system": {"topic_id": 7}}})
    n2 = Notifier(path=n.path, defaults=lambda: {}); n2.load()
    check("new settings survive a restart", (n2.cfg["low_disk_gb"], n2.cfg["low_disk_repeat_h"], n2.cfg["fail_streak"]) == (5, 2, 0)
          and n2.cfg["report"] == {"enabled": False, "time": "21:15", "utc_offset": 3.5, "last_sent": None})
    n.update({"low_disk_gb": 0})
    check("0 is a real value (warning off), not 'unchanged'", n.cfg["low_disk_gb"] == 0)
    n.mark_report_sent("2026-10-16")
    n3 = Notifier(path=n.path, defaults=lambda: {}); n3.load()
    check("last_sent is remembered (no second report after a restart)", n3.cfg["report"]["last_sent"] == "2026-10-16" and n3.cfg["report"]["time"] == "21:15")
    pub = n.public()
    check("public() shows the new settings", pub["low_disk_gb"] == 0 and pub["report"]["time"] == "21:15" and any(c["key"] == "reports" for c in pub["categories"]))

    # ---- the watcher: real loop, fake disk ----
    notify.notifier.__dict__.update(n.__dict__)
    free = {"v": 10 * GB}
    real = notify.shutil.disk_usage
    notify.shutil.disk_usage = lambda p: NS(total=80 * GB, used=80 * GB - free["v"], free=free["v"])
    try:
        notify.notifier.update({"low_disk_gb": 2.0, "low_disk_repeat_h": 6})
        task = asyncio.ensure_future(notify.watch_disk("/x", interval=0.05))

        async def step(free_gb, wait=0.2):
            free["v"] = int(free_gb * GB)
            await asyncio.sleep(wait)
            await notify.notifier._queue.join()

        await step(10)
        check("plenty of space: silent", not sent)
        await step(1.5)
        low = [s for s in sent if "Low disk space" in s["text"]]
        check("under 2 GB: one warning, with free space and what to do", len(low) == 1 and "1.5 GB" in low[0]["text"] and "Clean disk" in low[0]["text"] and low[0]["message_thread_id"] == 7, str(low[:1]))
        await step(1.4, 0.4)
        check("still low: no second warning inside the repeat window", len([s for s in sent if "Low disk space" in s["text"]]) == 1)
        clock["t"] += 6 * 3600 + 1
        await step(1.4, 0.3)
        check("still low after the repeat time: warns again", len([s for s in sent if "Low disk space" in s["text"]]) == 2)
        await step(2.1)
        check("just above the limit: no 'OK' yet (no flapping)", not [s for s in sent if "OK again" in s["text"]])
        await step(3.0)
        ok_msgs = [s for s in sent if "OK again" in s["text"]]
        check("clearly above the limit: 'Disk space is OK again'", len(ok_msgs) == 1 and "3.0 GB" in ok_msgs[0]["text"])
        await step(1.0)
        check("goes low again: warns again", len([s for s in sent if "Low disk space" in s["text"]]) == 3)
        notify.notifier.update({"low_disk_gb": 0})
        n_before = len(sent)
        await step(0.5)
        check("limit 0 switches the warning off", len(sent) == n_before)
        task.cancel()
    finally:
        notify.shutil.disk_usage = real
    print("ALL OK" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
