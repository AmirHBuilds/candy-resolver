"""Disk cleaner on a real temporary file tree. Run: python -m tests.smoke_diskclean"""
import os
import tempfile
import time
from pathlib import Path

from app.diskclean import delete, scan, summarize

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


def mk(path: Path, size=1000, age_s=0, dir_=False):
    if dir_:
        path.mkdir(parents=True, exist_ok=True)
        (path / "data.bin").write_bytes(b"x" * size)
        target = path / "data.bin"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        target = path
    t = time.time() - age_s
    os.utime(target, (t, t))
    os.utime(path, (t, t))
    return path


root = Path(tempfile.mkdtemp())
lib, tmp, data, outside = root / "library", root / "tmp", root / "data", root / "outside"
for d in (lib, tmp, data, outside):
    d.mkdir()
(data / "secrets.json").write_text('{"admin_token": "keep me"}')
(outside / "precious.txt").write_text("do not delete")
OLD, NEW = 7200, 10

active = "lib_" + "a" * 16
orphan = "lib_" + "b" * 16
fresh_orphan = "lib_" + "c" * 16
mk(lib / active, 5000, OLD, True)                     # alive: must stay even though old
mk(lib / orphan, 3000, OLD, True)                     # orphan, old: goes
mk(lib / fresh_orphan, 3000, NEW, True)               # orphan but just written: stays
mk(lib / "notes_by_admin", 100, OLD, True)            # not one of our ids: never touched
mk(lib / "stray.bin", 700, OLD)                       # stray old file: goes
mk(lib / "stray_new.bin", 700, NEW)                   # stray new file: stays
os.symlink(outside, lib / ("lib_" + "d" * 16))        # symlink to a folder outside: never followed

mk(tmp / "cr_script_abc", 2000, OLD, True)            # old script sandbox: goes
mk(tmp / "cr_script_running", 2000, NEW, True)        # recent: stays
mk(tmp / "playwright_chromiumdev_profile-xyz", 4000, OLD, True)
mk(tmp / ".org.chromium.Chromium.abc123", 1500, OLD, True)
mk(tmp / "my_own_folder", 900, OLD, True)             # not on the allowlist: stays
mk(tmp / "cr_script_busy", 2000, OLD, True)           # old dir, but something inside it was written a moment ago
mk(tmp / "cr_script_busy" / "log.txt", 10, NEW)
(tmp / ".X11-unix").mkdir()
dead_pid = 4_000_000
while True:
    try:
        os.kill(dead_pid, 0)
        dead_pid += 1
    except ProcessLookupError:
        break
    except PermissionError:
        dead_pid += 1
(tmp / ".X99-lock").write_text(f"{dead_pid:>10}\n"); (tmp / ".X11-unix" / "X99").write_text("")
(tmp / ".X98-lock").write_text(f"{os.getpid():>10}\n")   # its process is alive: stays

cache = root / "cache"
mk(cache / "http" / "blob", 6000, OLD)
mk(cache / "lock", 1, OLD)

cands = scan(lib, {active}, tmp, min_age_s=1800, cache_dirs=[cache])
paths = {Path(c.path).name for c in cands}
check("old orphan folder found", orphan in paths)
check("live item folder is never a candidate", active not in paths)
check("just-written orphan is left alone", fresh_orphan not in paths)
check("symlink in the library is skipped", ("lib_" + "d" * 16) not in paths)
check("folder with another name is skipped", "notes_by_admin" not in paths)
check("old stray file found, new one left", "stray.bin" in paths and "stray_new.bin" not in paths)
check("old script sandbox + browser leftovers found", {"cr_script_abc", "playwright_chromiumdev_profile-xyz", ".org.chromium.Chromium.abc123"} <= paths)
check("recent or busy temp folders and unknown names are left alone", not ({"cr_script_running", "cr_script_busy", "my_own_folder"} & paths))
check("dead X lock + its socket found, live one left", {".X99-lock", "X99"} <= paths and ".X98-lock" not in paths)
check("package cache contents found, 'lock' kept", "http" in paths and "lock" not in paths)

s = summarize(cands)
check("summary adds up", s["total_items"] == len(cands) and s["total_bytes"] == sum(c.bytes for c in cands) and s["categories"][0]["bytes"] >= s["categories"][-1]["bytes"])
before = {c.path for c in cands}
rep = delete(cands, [lib, tmp, cache])
check("delete removed everything it found, no errors", rep["removed"] == len(cands) and rep["errors"] == 0, str(rep["removed"]))
check("freed bytes match the scan", rep["freed_bytes"] == s["total_bytes"])
check("deleted things are gone", not any(os.path.lexists(p) for p in before))
check("live item, fresh files, unknown folders, symlink are still there",
      (lib / active / "data.bin").exists() and (lib / fresh_orphan).exists() and (lib / "notes_by_admin").exists()
      and (lib / "stray_new.bin").exists() and os.path.islink(lib / ("lib_" + "d" * 16)) and (tmp / "my_own_folder").exists()
      and (tmp / "cr_script_running").exists() and (tmp / ".X98-lock").exists() and (cache / "lock").exists())
check("nothing outside was touched (symlink target, secrets)", (outside / "precious.txt").read_text() == "do not delete" and (data / "secrets.json").exists())
check("the cache folder itself stays", cache.is_dir())

# safety net: a candidate outside the allowed roots is refused
from app.diskclean import Candidate
rep2 = delete([Candidate("orphan_library", str(outside / "precious.txt"), 13, False), Candidate("orphan_library", str(lib), 0, True)], [lib, tmp])
check("path outside the roots (and the root itself) is refused", rep2["removed"] == 0 and rep2["errors"] == 2 and (outside / "precious.txt").exists() and lib.exists(), str(rep2["errors"]))

# a second scan finds nothing left to do
check("second scan is empty", scan(lib, {active}, tmp, min_age_s=1800, cache_dirs=[cache]) == [])
print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
