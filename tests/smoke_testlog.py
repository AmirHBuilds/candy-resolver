"""Live test log with the REAL script runner (the app's config is stubbed so no web dependencies are needed).
Run: python -m tests.smoke_testlog"""
import asyncio
import sys
import time
import types
from types import SimpleNamespace as NS

cfg = types.ModuleType("app.config")
cfg.settings = NS(script_python=sys.executable, script_cpu_seconds=60, script_max_file_mb=50, script_max_open_files=256,
                  script_memory_mb=0, script_run_as_uid=None, script_run_as_gid=None, playwright_browsers_path="/tmp/pw",
                  script_max_output_bytes=1_000_000)
sys.modules["app.config"] = cfg

from app import testlog                                   # noqa: E402
from app.runner import ScriptError, run_script            # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


SLOW = '''
import time, sys, os
def resolve(ctx):
    print("step 1: start, title =", ctx["title"])
    time.sleep(1.0)
    print("step 2: héllo wörld ✓ 日本語")
    print("to stderr", file=sys.stderr)
    time.sleep(0.5)
    print("env DEBUG =", os.environ.get("DEBUG"))
    return {"streams": [{"url": "https://x/a.m3u8"}]}
'''


async def main():
    # --- live: the first line must be visible while the script is still sleeping ---
    run = testlog.create("demo", 1)
    seen = {}

    def on_log(data):
        run.feed(data)
        if run.lines and "first_at" not in seen:
            seen["first_at"] = time.monotonic()

    t0 = time.monotonic()
    result = await run_script(SLOW, {"title": "Obsession"}, 20, on_log=on_log, extra_env={"DEBUG": "pw:api"})
    run.finish("done", {"ok": True})
    total = time.monotonic() - t0
    texts = [l["text"] for l in run.lines]
    check("script result still returned", result == {"streams": [{"url": "https://x/a.m3u8"}]})
    check("print() lines are captured", "step 1: start, title = Obsession" in texts, str(texts[:2]))
    check("unicode survives", "step 2: héllo wörld ✓ 日本語" in texts)
    check("stderr is captured too", "to stderr" in texts)
    check("extra_env reaches the script", "env DEBUG = pw:api" in texts)
    check("lines arrive LIVE (first one long before the script ended)", seen["first_at"] - t0 < total - 1.0, f"first at {seen['first_at'] - t0:.2f}s of {total:.2f}s")
    check("timestamps increase", all(a["t"] <= b["t"] for a, b in zip(run.lines, run.lines[1:])))
    check("since(after) returns only new lines", len(run.since(2)["logs"]) == len(run.lines) - 2 and run.since(0)["next"] == len(run.lines))
    check("result only after finish", testlog.create("d", 1).since(0)["result"] is None and run.since(0)["result"] == {"ok": True})

    # --- a crashing script: traceback is in the log AND in the error ---
    run2 = testlog.create("demo", 2)
    try:
        await run_script('def resolve(ctx):\n    print("before crash")\n    raise ValueError("boom")\n', {}, 20, on_log=run2.feed)
        check("crash raises ScriptError", False)
    except ScriptError as e:
        text = "\n".join(l["text"] for l in run2.lines)
        check("crash: print before it + traceback in log", "before crash" in text and "ValueError: boom" in text and "ValueError: boom" in str(e))

    # --- without a listener nothing changes ---
    r = await run_script('def resolve(ctx):\n    print("quiet")\n    return {"streams": []}\n', {}, 20)
    check("works without on_log", r == {"streams": []})

    # --- a broken listener must not break the run ---
    def bad(_):
        raise RuntimeError("listener bug")
    r = await run_script('def resolve(ctx):\n    print("x")\n    return {"streams": []}\n', {}, 20, on_log=bad)
    check("broken listener does not break the script", r == {"streams": []})

    # --- stop button: cancelling kills the process ---
    run3 = testlog.create("demo", 3)
    task = asyncio.ensure_future(run_script('import time\ndef resolve(ctx):\n    print("sleeping", flush=True)\n    time.sleep(60)\n', {}, 120, on_log=run3.feed))
    await asyncio.sleep(1.0)
    check("log line visible while it sleeps", any(l["text"] == "sleeping" for l in run3.lines))
    t1 = time.monotonic()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("cancel returns quickly (process killed)", time.monotonic() - t1 < 6, f"{time.monotonic() - t1:.1f}s")

    # --- timeout keeps the log ---
    run4 = testlog.create("demo", 4)
    try:
        await run_script('import time\ndef resolve(ctx):\n    print("slow...", flush=True)\n    time.sleep(30)\n', {}, 2, on_log=run4.feed)
    except ScriptError as e:
        check("timeout: error + log has the last line", "timed out" in str(e) and any(l["text"] == "slow..." for l in run4.lines))

    # --- TestRun details ---
    r = testlog.TestRun("s", 1)
    r.feed("héllo".encode()[:2]); r.feed("héllo\nsecond".encode()[2:]); r.feed(b" line\r\nthird")
    r.finish("done", None)
    check("multi-byte split across chunks + CRLF + partial last line", [l["text"] for l in r.lines] == ["héllo", "second line", "third"], str([l["text"] for l in r.lines]))
    r = testlog.TestRun("s", 1)
    for i in range(testlog.MAX_LINES + 50):
        r.log("runner", f"l{i}")
    check("log is capped", len(r.lines) == testlog.MAX_LINES + 1 and "stopped" in r.lines[-1]["text"], str(len(r.lines)))
    for i in range(30):
        x = testlog.create("s", i); x.finish("done", None)
    check("old tests are forgotten", len(testlog._runs) <= testlog.KEEP_RUNS)

    print("ALL OK" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
