"""System monitoring for the admin panel (CPU, memory, disk, network, processes, health checks).

A background task samples every SAMPLE_EVERY seconds into a ring buffer so the charts already have history when
the panel is opened. Inside Docker, memory/CPU limits come from the container's cgroup, not from the host.
"""
import asyncio
import os
import platform
import shutil
import sys
import time
from collections import deque
from pathlib import Path

import psutil

from .config import settings

SAMPLE_EVERY = 5          # seconds
HISTORY = 360             # samples kept (= 30 minutes)
STARTED = time.time()
_history: deque = deque(maxlen=HISTORY)
_proc = psutil.Process()
_last_net = None
_task: asyncio.Task | None = None

# programs worth listing separately: what the resolver spawns
INTERESTING = ("ffmpeg", "ffprobe", "chrome", "chromium", "Xvfb", "node", "playwright", "python")


# ---------------------------------------------------------------- container (cgroup) limits
def _read_int(path: str) -> int | None:
    try:
        v = Path(path).read_text().strip()
        return None if v == "max" else int(v)
    except (OSError, ValueError):
        return None


def cgroup_memory() -> dict | None:
    """Memory used / limit of THIS container (cgroup v2, then v1). None when not in a cgroup or unlimited and unknown."""
    cur = _read_int("/sys/fs/cgroup/memory.current")
    lim = _read_int("/sys/fs/cgroup/memory.max")
    if cur is None:
        cur = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        lim = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if lim and lim > 1 << 60:
            lim = None
    if cur is None:
        return None
    total = psutil.virtual_memory().total
    if not lim or lim > total:
        lim = total
    return {"used": cur, "limit": lim, "percent": round(cur / lim * 100, 1)}


def _cpu_limit_cores() -> float | None:
    try:
        q, p = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        return None if q == "max" else round(int(q) / int(p), 2)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- sampling
_cache: dict[int, psutil.Process] = {}


def _tree_stats() -> dict:
    """CPU / memory of the API process and all of its descendants (ffmpeg, Chrome, Xvfb, scripts...).
    Process objects are cached by pid: psutil's cpu_percent() needs the previous call on the SAME object."""
    try:
        kids = _proc.children(recursive=True)
    except psutil.Error:
        kids = []
    live = {_proc.pid: _proc}
    for k in kids:
        live[k.pid] = _cache.get(k.pid) or k
    _cache.clear()
    _cache.update(live)
    procs = list(live.values())
    cpu = rss = 0.0
    for p in procs:
        try:
            cpu += p.cpu_percent(None)
            rss += p.memory_info().rss
        except psutil.Error:
            continue
    return {"cpu": round(cpu, 1), "rss": int(rss), "count": len(procs)}


def sample() -> dict:
    global _last_net
    now = time.time()
    vm = psutil.virtual_memory()
    cg = cgroup_memory()
    net = psutil.net_io_counters()
    rx = tx = 0.0
    if _last_net is not None:
        dt = max(now - _last_net[0], 0.001)
        rx, tx = (net.bytes_recv - _last_net[1]) / dt, (net.bytes_sent - _last_net[2]) / dt
    _last_net = (now, net.bytes_recv, net.bytes_sent)
    tree = _tree_stats()
    s = {
        "ts": now,
        "cpu": psutil.cpu_percent(None),                       # whole machine
        "mem": round((cg or {"percent": vm.percent})["percent"], 1),   # container if known, else machine
        "mem_used": (cg or {"used": vm.used})["used"],
        "app_cpu": tree["cpu"], "app_rss": tree["rss"], "app_procs": tree["count"],
        "rx": round(rx), "tx": round(tx),
    }
    _history.append(s)
    return s


async def _loop() -> None:
    psutil.cpu_percent(None)          # prime the counters
    psutil.cpu_percent(None, percpu=True)
    while True:
        try:
            await asyncio.to_thread(sample)
        except Exception as e:        # never let monitoring kill the app
            print("sysmon error:", e)
        await asyncio.sleep(SAMPLE_EVERY)


def start() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None


def history(n: int = HISTORY) -> list[dict]:
    return list(_history)[-n:]


# ---------------------------------------------------------------- snapshot for the panel
def snapshot() -> dict:
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        load = None
    settings.library_path.mkdir(parents=True, exist_ok=True)
    lib_du = shutil.disk_usage(settings.library_path)
    data_du = shutil.disk_usage(settings.data_dir) if Path(settings.data_dir).exists() else lib_du
    last = _history[-1] if _history else sample()
    try:
        fds = _proc.num_fds()
    except (psutil.Error, AttributeError):
        fds = None
    return {
        "now": time.time(),
        "uptime_s": int(time.time() - STARTED),
        "host": {"platform": platform.platform(), "python": sys.version.split()[0], "cores": psutil.cpu_count(),
                 "cpu_limit_cores": _cpu_limit_cores(), "load": load,
                 "boot_time": psutil.boot_time()},
        "cpu": {"percent": last["cpu"], "per_core": psutil.cpu_percent(None, percpu=True)},
        "memory": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent,
                   "swap_used": sw.used, "swap_total": sw.total, "container": cgroup_memory()},
        "disk": {"total": lib_du.total, "used": lib_du.used, "free": lib_du.free,
                 "percent": round(lib_du.used / lib_du.total * 100, 1) if lib_du.total else 0,
                 "data_dir": str(settings.data_dir)},
        "network": {"rx_per_s": last["rx"], "tx_per_s": last["tx"]},
        "app": {"cpu": last["app_cpu"], "rss": last["app_rss"], "procs": last["app_procs"], "fds": fds,
                "threads": _proc.num_threads(), "pid": _proc.pid},
    }


# ---------------------------------------------------------------- processes
def list_processes(limit: int = 40) -> list[dict]:
    """The API process and everything it started, plus any other heavy/interesting process in the container."""
    mine = {_proc.pid}
    try:
        mine |= {c.pid for c in _proc.children(recursive=True)}
    except psutil.Error:
        pass
    rows = []
    for p in psutil.process_iter(["pid", "ppid", "name", "username", "create_time", "status"]):
        try:
            info = p.info
            name = info["name"] or ""
            if info["pid"] not in mine and not any(k.lower() in name.lower() for k in INTERESTING[:6]):
                continue
            with p.oneshot():
                cmd = " ".join(p.cmdline())[:300]
                rows.append({
                    "pid": info["pid"], "ppid": info["ppid"], "name": name, "user": info["username"],
                    "status": info["status"], "cpu": p.cpu_percent(None), "rss": p.memory_info().rss,
                    "age_s": int(time.time() - info["create_time"]), "cmd": cmd,
                    "child": info["pid"] in mine and info["pid"] != _proc.pid, "self": info["pid"] == _proc.pid,
                })
        except psutil.Error:
            continue
    rows.sort(key=lambda r: (-int(r["self"]), -r["rss"]))
    return rows[:limit]


def kill_process(pid: int) -> str:
    """Kill one process, but ONLY a descendant of the API process (never the API itself, never anything else)."""
    if pid == _proc.pid:
        raise PermissionError("refusing to kill the API process itself")
    try:
        target = psutil.Process(pid)
        if pid not in {c.pid for c in _proc.children(recursive=True)}:
            raise PermissionError("only processes started by the resolver can be killed here")
        name = target.name()
        for c in target.children(recursive=True):
            try:
                c.kill()
            except psutil.Error:
                pass
        target.kill()
        return name
    except psutil.NoSuchProcess:
        raise LookupError("process already gone")


# ---------------------------------------------------------------- health checks
def _version(cmd: str) -> str | None:
    import subprocess
    try:
        out = subprocess.run([cmd, "-version"], capture_output=True, text=True, timeout=5).stdout
        return out.splitlines()[0][:80] if out else None
    except Exception:
        return None


def tool_checks() -> list[dict]:
    chrome = "/opt/google/chrome/chrome"
    pw_path = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or settings.playwright_browsers_path)
    pw_browsers = sorted(p.name for p in pw_path.iterdir()) if pw_path.is_dir() else []
    try:
        import playwright  # noqa: F401
        pw_ok = True
    except ImportError:
        pw_ok = False
    try:
        import pyvirtualdisplay  # noqa: F401
        pvd_ok = True
    except ImportError:
        pvd_ok = False
    ff = shutil.which(settings.ffmpeg_path)
    fp = shutil.which(settings.ffprobe_path)
    xvfb = shutil.which("Xvfb")
    uid = settings.script_run_as_uid
    return [
        {"name": "ffmpeg", "ok": bool(ff), "detail": (_version(ff) if ff else "not installed - downloads will fail")},
        {"name": "ffprobe", "ok": bool(fp), "detail": fp or "not installed - progressive (play-while-downloading) will fail"},
        {"name": "Xvfb", "ok": bool(xvfb), "detail": xvfb or "not installed - pyvirtualdisplay sources will fail"},
        {"name": "Google Chrome", "ok": os.path.exists(chrome),
         "detail": chrome if os.path.exists(chrome) else "missing - sources using channel=\"chrome\" will fail (amd64 only)"},
        {"name": "Playwright browsers", "ok": bool(pw_browsers), "detail": f"{pw_path}: {', '.join(pw_browsers) or 'none installed'}"},
        {"name": "playwright (python)", "ok": pw_ok, "detail": "importable" if pw_ok else "pip package missing"},
        {"name": "pyvirtualdisplay (python)", "ok": pvd_ok, "detail": "importable" if pvd_ok else "pip package missing"},
        {"name": "/dev/shm", "ok": _shm_ok(), "detail": _shm_detail()},
        {"name": "Script sandbox", "ok": uid is not None,
         "detail": f"scripts run as uid {uid}" if uid is not None else "scripts run as the same user as the API (no sandbox)"},
        {"name": "TMDB key", "ok": bool(settings.tmdb_api_key), "detail": "configured" if settings.tmdb_api_key else "TMDB_API_KEY is empty"},
        {"name": "Public base URL", "ok": True,
         "detail": settings.public_base_url or "not set (optional): links use the host of the incoming request"},
    ]


def _shm_ok() -> bool:
    try:
        return shutil.disk_usage("/dev/shm").total >= 256 * 1024 * 1024
    except OSError:
        return True


def _shm_detail() -> str:
    try:
        t = shutil.disk_usage("/dev/shm").total
        return f"{t // (1024 * 1024)} MB" + ("" if t >= 256 * 1024 * 1024 else " - too small for Chrome (set shm_size: 1gb)")
    except OSError:
        return "n/a"
