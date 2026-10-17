"""Runs an uploaded source script in an isolated, resource-limited subprocess.

Contract for scripts: define `resolve(ctx)` (sync or async) that returns a JSON-serialisable dict:
    {"streams": [{"url", "quality", "format", "size", "headers", "extra"}...],
     "subtitles": [{"lang", "url", "format"}...], "audio": [{"lang"}...]}
`ctx` is the TMDB metadata dict (title, alt_titles, year, imdb_id, season, episode, ...).
"""
import asyncio
import json
import os
import shutil
import signal
import tempfile

from .config import settings


class ScriptError(Exception):
    pass


class _OutputTooLarge(Exception):
    pass


# Executed inside the child process. Anything the script print()s goes to stderr,
# so only the final JSON result lands on stdout.
HARNESS = r'''
import asyncio, importlib.util, inspect, json, sys, traceback

def main():
    real_out = sys.stdout
    sys.stdout = sys.stderr
    ctx = json.load(sys.stdin)
    spec = importlib.util.spec_from_file_location("source_script", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "resolve", None)
    if fn is None:
        raise RuntimeError("script does not define resolve(ctx)")
    result = asyncio.run(fn(ctx)) if inspect.iscoroutinefunction(fn) else fn(ctx)
    real_out.write(json.dumps(result))
    real_out.flush()

try:
    main()
except Exception:
    traceback.print_exc()
    sys.exit(1)
'''


def _make_preexec(uid: int | None = None, gid: int | None = None):
    """Runs in the child just before exec: resource limits, then (optionally) drop to an unprivileged user.
    This is done here instead of subprocess's user=/group= because uvloop (used by uvicorn) rejects those."""
    if os.name != "posix":
        return None
    import resource

    limits = [
        (resource.RLIMIT_CPU, settings.script_cpu_seconds),
        (resource.RLIMIT_FSIZE, settings.script_max_file_mb * 1024 * 1024),
        (resource.RLIMIT_NOFILE, settings.script_max_open_files),
        (resource.RLIMIT_CORE, 0),
    ]
    if settings.script_memory_mb > 0:  # optional; breaks browsers, so off by default
        limits.insert(0, (resource.RLIMIT_AS, settings.script_memory_mb * 1024 * 1024))

    def apply():
        for lim, val in limits:
            try:
                resource.setrlimit(lim, (val, val))
            except (ValueError, OSError):
                pass  # limit not supported on this platform
        if uid is not None:  # last, because it cannot be undone
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

    return apply


async def _read_capped(stream, cap: int, strict: bool, on_chunk=None) -> bytes:
    """Read a pipe to EOF, keeping at most `cap` bytes.
    strict=True raises when the cap is exceeded; strict=False silently drops the excess
    (we still drain the pipe so the child never blocks on a full buffer).
    on_chunk(bytes) sees every chunk the moment it arrives (live log), before any cap is applied."""
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf)
        if on_chunk is not None:
            try:
                on_chunk(chunk)
            except Exception:
                pass                                  # a broken log listener must never break the script run
        room = cap - len(buf)
        if len(chunk) > room:
            if strict:
                raise _OutputTooLarge()
            chunk = chunk[: max(room, 0)]
        buf.extend(chunk)


def _kill(proc):
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def _tail(data: bytes, n: int = 1500) -> str:
    return data.decode("utf-8", "replace").strip()[-n:]


def _measure_tree(root) -> tuple[int, int]:
    """(bytes, process count) used by a process and all its children. PSS where possible: Chrome's processes share
    pages, and summing plain RSS would count them several times."""
    import psutil
    try:
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        procs = [root]
    total = n = 0
    for p in procs:
        try:
            try:
                total += p.memory_full_info().pss
            except (AttributeError, psutil.AccessDenied):
                total += p.memory_info().rss
            n += 1
        except psutil.Error:
            continue
    return total, n


async def _watch_peak(pid: int, stats: dict) -> None:
    import psutil
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    peak = 0
    while True:
        try:
            total, n = await asyncio.to_thread(_measure_tree, root)
        except Exception:
            return
        if total > peak:
            peak = total
            stats["peak_mb"] = max(1, round(peak / 1048576))
        stats["peak_procs"] = max(stats.get("peak_procs", 0), n)
        await asyncio.sleep(0.5)


async def run_script(code: str, ctx: dict, timeout_s: int, stats: dict | None = None, on_log=None, extra_env: dict | None = None) -> dict:
    """Run one source script in a sandboxed subprocess. If `stats` is given it is filled with peak_mb / peak_procs.
    on_log(bytes) is called live with everything the script prints (print(), logging, tracebacks: all go to stderr).
    extra_env adds environment variables (only used for the admin test, e.g. DEBUG=pw:api)."""
    tmp = tempfile.mkdtemp(prefix="cr_script_")
    proc = None
    watcher = None
    try:
        script_path = os.path.join(tmp, "source_script.py")
        harness_path = os.path.join(tmp, "harness.py")
        for path, text in ((script_path, code), (harness_path, HARNESS)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

        uid = settings.script_run_as_uid
        gid = settings.script_run_as_gid if settings.script_run_as_gid is not None else uid
        if uid is not None:
            if os.geteuid() != 0:
                raise ScriptError("SCRIPT_RUN_AS_UID is set but the server is not running as root")
            for p in (tmp, script_path, harness_path):
                os.chown(p, uid, gid)
            os.chmod(tmp, 0o700)

        # Scrubbed environment: no app secrets, DB URL, or API keys reach the script.
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": tmp,
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            # Playwright: where the browsers live (shared, readable by the unprivileged user)
            "PLAYWRIGHT_BROWSERS_PATH": settings.playwright_browsers_path,
            "XDG_RUNTIME_DIR": tmp,
            "TMPDIR": "/tmp",
        }
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})

        proc = await asyncio.create_subprocess_exec(
            settings.script_python, "-I", harness_path, script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp,
            env=env,
            start_new_session=True,  # own process group so we can kill children too
            preexec_fn=_make_preexec(uid, gid),
        )

        if stats is not None:
            watcher = asyncio.ensure_future(_watch_peak(proc.pid, stats))
        payload = json.dumps(ctx).encode()

        async def feed():
            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        async def collect():
            _, out, err, rc = await asyncio.gather(
                feed(),
                _read_capped(proc.stdout, settings.script_max_output_bytes, True),
                _read_capped(proc.stderr, 64 * 1024, False, on_log),
                proc.wait(),
            )
            return out, err, rc

        try:
            out, err, rc = await asyncio.wait_for(collect(), timeout_s)
        except asyncio.TimeoutError:
            raise ScriptError(f"timed out after {timeout_s}s")
        except _OutputTooLarge:
            raise ScriptError("script output too large")

        if rc != 0:
            if rc < 0:
                raise ScriptError(f"script was killed (signal {-rc}) - probably hit the memory/CPU limit")
            raise ScriptError(_tail(err) or f"script exited with code {rc}")
        try:
            return json.loads(out)
        except ValueError:
            raise ScriptError("script did not return valid JSON")
    finally:
        if watcher is not None:
            watcher.cancel()
        if proc is not None and proc.returncode is None:
            _kill(proc)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
