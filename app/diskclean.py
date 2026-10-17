"""Disk cleaner: finds and removes leftovers nothing needs. Filesystem logic only (no database, no web framework).

It works from an ALLOWLIST: only the kinds of leftovers named below are ever touched, only inside the roots passed in,
never through symlinks, and never anything recently written (a running download or browser must not lose its files).
Library folders that belong to a live item are given by the caller (active_ids) and are never candidates."""
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

LIB_ID = re.compile(r"lib_[0-9a-f]{16}")
SCRIPT_TMP = re.compile(r"cr_script_.+")
BROWSER_TMP = re.compile(
    r"playwright_chromiumdev_profile-.+|playwright-artifacts-.+|playwright-(?:chromium|firefox|webkit)-.+"
    r"|\.org\.chromium\.(?:Chromium|Chrome)\..+|\.com\.google\.Chrome\..+|chrome_crashpad_.+|scoped_dir.+|pyvirtualdisplay_.+")
X_LOCK = re.compile(r"\.X(\d+)-lock")

LABELS = {
    "orphan_library": "Library folders nothing points at",
    "stray_library_file": "Stray files in the library folder",
    "script_temp": "Leftover script sandboxes",
    "browser_temp": "Leftover browser profiles and temp files",
    "x_locks": "Stale virtual-display locks",
    "package_cache": "Package caches (pip / apt)",
}
ORPHAN_MIN_AGE_S = 60          # a folder written in the last minute may belong to a download that is just starting
_WALK_CAP = 20000              # entries looked at per folder when measuring (keeps a scan fast on huge folders)


@dataclass
class Candidate:
    category: str
    path: str
    bytes: int
    is_dir: bool


def _tree(path: str) -> tuple[int, float]:
    """(total bytes, newest modification time) of a file or a folder tree. Symlinks are measured, never followed."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0.0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_size, st.st_mtime
    total, newest, seen, stack = 0, st.st_mtime, 0, [path]
    while stack and seen < _WALK_CAP:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    seen += 1
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += s.st_size
                    newest = max(newest, s.st_mtime)
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
        except OSError:
            continue
    return total, newest


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def scan(library_dir, active_ids: set[str], tmp_dir, now: float | None = None, min_age_s: float = 1800,
         cache_dirs=()) -> list[Candidate]:
    now = now or time.time()
    out: list[Candidate] = []

    # 1. library: folders no live item points at, and stray files
    lib = Path(library_dir)
    if lib.is_dir():
        with os.scandir(lib) as it:
            for e in it:
                if e.is_symlink():
                    continue
                size, newest = _tree(e.path)
                if e.is_dir(follow_symlinks=False):
                    if LIB_ID.fullmatch(e.name) and e.name not in active_ids and now - newest > ORPHAN_MIN_AGE_S:
                        out.append(Candidate("orphan_library", e.path, size, True))
                elif now - newest > min_age_s:
                    out.append(Candidate("stray_library_file", e.path, size, False))

    # 2. temp dir: leftovers of scripts and browsers, old enough that nothing can still be using them
    tmp = Path(tmp_dir)
    if tmp.is_dir():
        with os.scandir(tmp) as it:
            for e in it:
                if e.is_symlink():
                    continue
                cat = "script_temp" if SCRIPT_TMP.fullmatch(e.name) else "browser_temp" if BROWSER_TMP.fullmatch(e.name) else None
                m = X_LOCK.fullmatch(e.name)
                if cat:
                    size, newest = _tree(e.path)
                    if now - newest > min_age_s:
                        out.append(Candidate(cat, e.path, size, e.is_dir(follow_symlinks=False)))
                elif m and e.is_file(follow_symlinks=False):
                    try:
                        pid = int(Path(e.path).read_text().strip())
                    except (OSError, ValueError):
                        continue
                    if not _pid_alive(pid):
                        out.append(Candidate("x_locks", e.path, os.lstat(e.path).st_size, False))
                        sock = tmp / ".X11-unix" / f"X{m.group(1)}"
                        if sock.exists() and not sock.is_symlink():
                            out.append(Candidate("x_locks", str(sock), 0, False))

    # 3. package caches: contents only, the folder stays
    for d in cache_dirs:
        d = Path(d)
        if not d.is_dir() or d.is_symlink():
            continue
        with os.scandir(d) as it:
            for e in it:
                if e.name in ("lock", "partial") or e.is_symlink():
                    continue
                size, _ = _tree(e.path)
                out.append(Candidate("package_cache", e.path, size, e.is_dir(follow_symlinks=False)))
    return out


def summarize(cands: list[Candidate], samples: int = 5) -> dict:
    cats: dict[str, dict] = {}
    for c in cands:
        d = cats.setdefault(c.category, {"category": c.category, "label": LABELS.get(c.category, c.category), "items": 0, "bytes": 0, "samples": []})
        d["items"] += 1
        d["bytes"] += c.bytes
        if len(d["samples"]) < samples:
            d["samples"].append(c.path)
    rows = sorted(cats.values(), key=lambda r: -r["bytes"])
    return {"categories": rows, "total_items": sum(r["items"] for r in rows), "total_bytes": sum(r["bytes"] for r in rows)}


def _inside(path: str, roots) -> bool:
    real = os.path.realpath(os.path.dirname(path))                 # the parent is resolved; the entry itself is never followed
    full = os.path.join(real, os.path.basename(path))
    for r in roots:
        root = os.path.realpath(str(r))
        if full != root and full.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def delete(cands: list[Candidate], roots) -> dict:
    """Remove the candidates. Anything outside `roots`, or that turned into a symlink, is skipped. Returns the report."""
    report: dict[str, dict] = {}
    for c in cands:
        r = report.setdefault(c.category, {"category": c.category, "label": LABELS.get(c.category, c.category),
                                           "removed": 0, "freed_bytes": 0, "errors": []})
        try:
            if not _inside(c.path, roots):
                raise PermissionError("outside the allowed folders")
            if os.path.islink(c.path):
                os.unlink(c.path)
            elif os.path.isdir(c.path):
                shutil.rmtree(c.path)
            elif os.path.lexists(c.path):
                os.unlink(c.path)
            else:
                continue                                          # already gone
            r["removed"] += 1
            r["freed_bytes"] += c.bytes
        except Exception as e:
            if len(r["errors"]) < 10:
                r["errors"].append(f"{c.path}: {e}")
    rows = sorted(report.values(), key=lambda x: -x["freed_bytes"])
    return {"categories": rows, "removed": sum(r["removed"] for r in rows), "freed_bytes": sum(r["freed_bytes"] for r in rows),
            "errors": sum(len(r["errors"]) for r in rows)}
