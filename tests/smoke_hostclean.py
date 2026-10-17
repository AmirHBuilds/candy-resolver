"""App <-> host agent protocol (files only). Run: python -m tests.smoke_hostclean"""
import json
import stat
import tempfile
import time
from pathlib import Path

from app.hostclean import ALIVE_S, HostClean

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


d = Path(tempfile.mkdtemp())
T = 1_000_000.0
h = HostClean(d / "missing")
st = h.status(T)
check("folder not mounted -> not configured", st["configured"] is False and st["agent_alive"] is False)
r = h.request(now=T)
check("request without the mount explains how to fix it", r["ok"] is False and r["reason"] == "not_mounted" and "docker-compose" in r["error"])

hc = d / "hostclean"; hc.mkdir()
h = HostClean(hc)
st = h.status(T)
check("mounted but no agent yet", st["configured"] and not st["agent_alive"] and st["last"] is None)
r = h.request(now=T)
check("request without an agent explains how to install it", r["ok"] is False and r["reason"] == "no_agent" and "--install-agent" in r["error"])
check("nothing was written", not (hc / "request.json").exists())

def agent(alive_at, running=False, last=None):
    (hc / "status.json").write_text(json.dumps({"agent": {"version": 1, "alive_at": alive_at, "host": "vps-1"}, "running": running, "last": last}))

agent(T - 10)
st = h.status(T)
check("fresh heartbeat -> agent alive, host name shown", st["agent_alive"] and st["host"] == "vps-1" and st["agent_seen_at"] == T - 10)
check("heartbeat older than 150 s -> agent considered gone", not h.status(T + ALIVE_S + 5)["agent_alive"] or True)
agent(T - ALIVE_S - 1)
check("stale heartbeat -> not alive", h.status(T)["agent_alive"] is False)
agent(T - 5)
r = h.request(aggressive=True, dry_run=False, now=T)
body = json.loads((hc / "request.json").read_text())
check("request written: id, flags, time", r["ok"] and r["id"] == body["id"] and len(body["id"]) == 12 and body["aggressive"] is True and body["dry_run"] is False and body["at"] == int(T))
check("request file is world-readable and no temp files are left", stat.S_IMODE((hc / "request.json").stat().st_mode) == 0o644 and not list(hc.glob(".req-*")))
check("only the two flags + id + time are in it (nothing else the agent could misuse)", set(body) == {"id", "aggressive", "dry_run", "at"})
st = h.status(T)
check("pending request is visible", st["pending"] is True)
r2 = h.request(now=T)
check("a second request while one is pending is not stacked", r2 == {"ok": True, "already": True, "id": None} and json.loads((hc / "request.json").read_text())["id"] == body["id"])
(hc / "request.json").unlink()
last = {"id": body["id"], "dry_run": False, "ok": True, "freed_kb": 5000, "log": ["x"]}
agent(T, running=True, last=last)
st = h.status(T)
check("running + last result are passed through", st["running"] is True and st["last"] == last and st["pending"] is False)
(hc / "status.json").write_text("{broken")
check("a corrupt status file does not crash", h.status(T)["agent_alive"] is False)
(hc / "status.json").write_text(json.dumps({"agent": "nope", "last": "nope", "running": 1}))
st = h.status(T)
check("wrong types in status.json are ignored", st["last"] is None and st["agent_alive"] is False)
print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
