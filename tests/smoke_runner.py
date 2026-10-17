"""Quick check that the script sandbox behaves. Run: python -m tests.smoke_runner"""
import asyncio
import time

from app.runner import ScriptError, run_script

CTX = {"title": "Test", "year": 2000}


async def expect_error(name, code, needle, timeout=3):
    t0 = time.monotonic()
    try:
        await run_script(code, CTX, timeout)
    except ScriptError as e:
        ok = needle in str(e)
        print(("PASS" if ok else "FAIL"), name, f"({time.monotonic()-t0:.1f}s)", "->", str(e)[:80].replace("\n", " "))
        return ok
    print("FAIL", name, "- no error raised")
    return False


async def main():
    results = []

    out = await run_script('def resolve(ctx):\n    print("noise")\n    return {"streams":[{"url":"https://x/y.mp4","title":ctx["title"]}]}', CTX, 5)
    ok = out["streams"][0]["title"] == "Test"
    print("PASS" if ok else "FAIL", "normal run + stray print ignored"); results.append(ok)

    out = await run_script('async def resolve(ctx):\n    return {"streams": []}', CTX, 5)
    ok = out == {"streams": []}
    print("PASS" if ok else "FAIL", "async resolve"); results.append(ok)

    results.append(await expect_error("infinite loop -> timeout", "def resolve(ctx):\n    while True: pass", "timed out", 2))
    results.append(await expect_error("exception surfaces", "def resolve(ctx):\n    raise ValueError('boom')", "boom"))
    results.append(await expect_error("memory hog is stopped", "def resolve(ctx):\n    x = bytearray(2*1024**3)\n    return {}", "MemoryError"))
    results.append(await expect_error("huge output rejected", "def resolve(ctx):\n    return {'a': 'x'*3_000_000}", "too large"))
    results.append(await expect_error("no resolve()", "x = 1", "resolve"))
    results.append(await expect_error("not json", "def resolve(ctx):\n    return {1, 2}", "TypeError"))

    code = ("import os\n"
            "def resolve(ctx):\n"
            "    return {'env_keys': sorted(os.environ.keys())}")
    out = await run_script(code, CTX, 5)
    ok = not any(k in out["env_keys"] for k in ("TMDB_API_KEY", "ADMIN_TOKEN", "DATABASE_URL"))
    print("PASS" if ok else "FAIL", "env scrubbed:", out["env_keys"]); results.append(ok)

    import os
    if os.name == "posix" and os.geteuid() == 0:
        from app.config import settings
        secret = "/tmp/cr_root_only_secret"
        with open(secret, "w") as f:
            f.write("top secret")
        os.chmod(secret, 0o600)
        settings.script_run_as_uid = settings.script_run_as_gid = 65534
        try:
            code = ("import os\n"
                    "def resolve(ctx):\n"
                    "    try:\n"
                    f"        open({secret!r}).read(); leaked = True\n"
                    "    except PermissionError:\n"
                    "        leaked = False\n"
                    "    return {'uid': os.getuid(), 'leaked': leaked}")
            out = await run_script(code, CTX, 10)
            ok = out["uid"] == 65534 and out["leaked"] is False
            print("PASS" if ok else "FAIL", "runs as nobody, cannot read root-only file:", out)
            results.append(ok)
        finally:
            settings.script_run_as_uid = settings.script_run_as_gid = None
            os.remove(secret)
    else:
        print("SKIP uid-drop test (needs root)")

    print("\nALL PASSED" if all(results) else "\nSOME FAILED")


if __name__ == "__main__":
    asyncio.run(main())
