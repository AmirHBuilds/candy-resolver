"""Export / import of sources: file checks and the plan. Pure logic. Run: python -m tests.smoke_sourcexfer"""
import ast
import json

from app.sourcexfer import FORMAT, build_export, code_hash, free_name, parse_import, plan

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print("PASS" if cond else "FAIL", name, extra)


def validate(code):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"syntax error on line {e.lineno}")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "resolve" for n in tree.body):
        raise ValueError("script must define a top-level resolve(ctx) function")


C1, C2, C3 = "def resolve(ctx):\n    return {'streams': []}\n", "def resolve(ctx):\n    return {'streams': [1]}\n", "def resolve(ctx):\n    return {}\n"
db = [
    {"name": "vertex", "public_name": "Server A", "base_url": "https://v", "language": "en", "enabled": True, "starred": True, "rank": 1,
     "timeout_s": 60, "uses_browser": True, "browser_manual": False,
     "scripts": [{"version": 1, "note": "first", "active": False, "created_at": "2026-01-01", "code": C1},
                 {"version": 2, "note": "fix", "active": True, "created_at": "2026-02-01", "code": C2}]},
    {"name": "archive", "public_name": "", "base_url": "", "language": "en", "enabled": False, "starred": False, "rank": None,
     "timeout_s": 30, "uses_browser": False, "browser_manual": True, "scripts": []},
]

# ---- export ----
e_act = build_export(db, "active", "1.0.5", "2026-10-16T00:00:00Z")
e_all = build_export(db, "all", "1.0.5", "2026-10-16T00:00:00Z")
check("format header", e_act["format"] == FORMAT and e_act["version"] == 1 and e_act["app_version"] == "1.0.5")
check("include=active keeps only the active version", [s["version"] for s in e_act["sources"][0]["scripts"]] == [2])
check("include=all keeps every version in order", [s["version"] for s in e_all["sources"][0]["scripts"]] == [1, 2])
check("a source without scripts is exported", e_act["sources"][1]["scripts"] == [])
check("no ids or stats in the file", "id" not in e_act["sources"][0] and "last_ms" not in json.dumps(e_act))
no_active = build_export([{**db[0], "scripts": [{"version": 1, "active": False, "code": C1}, {"version": 2, "active": False, "code": C2}]}], "active", "x", "t")
check("no active version -> the newest is exported", no_active["sources"][0]["scripts"][0]["version"] == 2)

# ---- round trip ----
entries, problems = parse_import(json.loads(json.dumps(e_all)), validate)
check("round trip: nothing lost, nothing flagged", len(entries) == 2 and not problems)
v = entries[0]
check("settings survive", (v["name"], v["public_name"], v["starred"], v["rank"], v["timeout_s"], v["uses_browser"]) == ("vertex", "Server A", True, 1, 60, True))
check("versions in order, exactly one active (the exported active one)", [s["code"] for s in v["scripts"]] == [C1, C2] and [s["active"] for s in v["scripts"]] == [False, True])
check("disabled source stays disabled", entries[1]["enabled"] is False and entries[1]["browser_manual"] is True)

# ---- hostile / broken files ----
def refuses(data, word):
    try:
        parse_import(data, validate)
        return False
    except ValueError as e:
        return word in str(e)

check("random JSON is refused", refuses({"hello": 1}, "not a candyresolver"))
check("list instead of object is refused", refuses([1, 2], "not a candyresolver"))
check("newer file version is refused", refuses({"format": FORMAT, "version": 99, "sources": [{}]}, "newer"))
check("empty sources is refused", refuses({"format": FORMAT, "version": 1, "sources": []}, "no sources"))
mixed = {"format": FORMAT, "version": 1, "sources": [
    {"name": "good", "scripts": [{"version": 1, "code": C1, "active": True}]},
    {"name": "badsyntax", "scripts": [{"version": 1, "code": "def resolve(ctx:\n", "active": True}]},
    {"name": "noresolve", "scripts": [{"version": 1, "code": "x = 1\n"}]},
    {"name": "nocode", "scripts": [{"version": 1, "code": ""}]},
    {"name": "good", "scripts": []},
    {"scripts": []}, "junk", 5,
    {"name": "weird", "public_name": 5, "rank": "1", "timeout_s": 99999, "enabled": "yes", "starred": 1, "scripts": "nope"}]}
entries, problems = parse_import(mixed, validate)
names = [e["name"] for e in entries]
check("good sources are kept, bad ones reported one by one", names == ["good", "weird"] and len(problems) == 7, f"{names} {[p['name'] for p in problems]}")
w = entries[1]
check("wrong types fall back to safe defaults", (w["public_name"], w["rank"], w["timeout_s"], w["enabled"], w["starred"], w["scripts"]) == ("", None, 30, True, False, []))
check("syntax error is explained", any("syntax error" in p["error"] for p in problems) and any("resolve" in p["error"] for p in problems))
check("duplicate name in the file is flagged", any("twice" in p["error"] for p in problems))
long = {"format": FORMAT, "version": 1, "sources": [{"name": "n" * 300, "base_url": "u" * 900, "scripts": []}]}
e2, _ = parse_import(long, validate)
check("overlong strings are cut to the database limits", len(e2[0]["name"]) == 100 and len(e2[0]["base_url"]) == 500)
two_active = {"format": FORMAT, "version": 1, "sources": [{"name": "a", "scripts": [{"version": 1, "code": C1, "active": True}, {"version": 2, "code": C2, "active": True}]}]}
check("two active flags -> only the last stays active", [s["active"] for s in parse_import(two_active, validate)[0][0]["scripts"]] == [False, True])
unsorted = {"format": FORMAT, "version": 1, "sources": [{"name": "a", "scripts": [{"version": 5, "code": C2}, {"version": 2, "code": C1}]}]}
check("versions are sorted when the file is not", [s["code"] for s in parse_import(unsorted, validate)[0][0]["scripts"]] == [C1, C2])

# ---- plan ----
entries, _ = parse_import(json.loads(json.dumps(e_all)), validate)
existing = {"vertex": {code_hash(C1)}}
p = plan(entries, existing, "skip")
check("skip: existing untouched, unknown created", [(x["action"], x["final_name"]) for x in p] == [("skip", "vertex"), ("create", "archive")])
p = plan(entries, existing, "update")
check("update: only scripts it does not have yet are added", p[0]["action"] == "update" and [s["code"] for s in p[0]["new_scripts"]] == [C2], p[0]["detail"])
p = plan(entries, {"vertex": {code_hash(C1), code_hash(C2 + "   \r\n")}}, "update")
check("update: same script (even with other line endings) is not added twice", p[0]["new_scripts"] == [] and "already there" in p[0]["detail"])
p = plan(entries, existing, "copy")
check("copy: new name, all scripts", p[0]["action"] == "copy" and p[0]["final_name"] == "vertex (imported)" and len(p[0]["new_scripts"]) == 2)
check("copy names never collide", free_name("vertex", {"vertex", "vertex (imported)", "vertex (imported 2)"}) == "vertex (imported 3)")
e3, _ = parse_import({"format": FORMAT, "version": 1, "sources": [{"name": "a", "scripts": []}, {"name": "b", "scripts": []}]}, validate)
p = plan(e3, {"a": set(), "b": set(), "a (imported)": set()}, "copy")
check("copy names do not collide with each other either", [x["final_name"] for x in p] == ["a (imported 2)", "b (imported)"])
check("code_hash ignores line endings and outer blanks", code_hash("x = 1\n") == code_hash("  x = 1\r\n\n".strip()) == code_hash("x = 1"))
print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
