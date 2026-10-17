"""Export / import of sources (settings + scripts) as one JSON file. Pure logic: no database, no web framework.

File format {"format": "candyresolver-sources", "version": 1, "sources": [{name, public_name, base_url, language, enabled,
starred, rank, timeout_s, uses_browser, browser_manual, scripts: [{version, note, active, created_at, code}]}]}.
Nothing server-specific travels in it: no ids, no task history, no statistics, no API keys."""
import hashlib

FORMAT = "candyresolver-sources"
VERSION = 1
MAX_SOURCES = 500
MAX_SCRIPTS = 100


def code_hash(code: str) -> str:
    """Same script text -> same hash (line endings and trailing blanks do not count)."""
    return hashlib.sha256(code.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


def build_export(sources: list[dict], include: str, app_version: str, exported_at: str) -> dict:
    """sources = plain dicts (see format). include='active' keeps one script per source (the active one), 'all' keeps every version."""
    out = []
    for s in sources:
        scripts = sorted(s.get("scripts") or [], key=lambda x: x["version"])
        if include != "all" and scripts:
            scripts = [next((x for x in scripts if x.get("active")), scripts[-1])]
        out.append({**{k: s.get(k) for k in ("name", "public_name", "base_url", "language", "enabled", "starred", "rank",
                                              "timeout_s", "uses_browser", "browser_manual")},
                    "scripts": [{"version": x["version"], "note": x.get("note") or "", "active": bool(x.get("active")),
                                 "created_at": x.get("created_at"), "code": x["code"]} for x in scripts]})
    return {"format": FORMAT, "version": VERSION, "exported_at": exported_at, "app_version": app_version, "sources": out}


def _str(v, default="", limit=500) -> str:
    return v.strip()[:limit] if isinstance(v, str) else default


def _bool(v, default=False) -> bool:
    return v if isinstance(v, bool) else default


def _int(v, lo, hi, default):
    if isinstance(v, bool) or not isinstance(v, int):
        return default
    return v if lo <= v <= hi else default


def parse_import(data, validate) -> tuple[list[dict], list[dict]]:
    """Check an uploaded file. Returns (clean entries, problems). One bad source never stops the others.
    validate(code) must raise ValueError(message) for a script that is not acceptable."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("this is not a candyresolver sources file (export one from the Sources page)")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise ValueError("this file was made by a newer version of candyresolver: update the server first")
    raw = data.get("sources")
    if not isinstance(raw, list) or not raw:
        raise ValueError("the file contains no sources")
    if len(raw) > MAX_SOURCES:
        raise ValueError(f"too many sources in one file (max {MAX_SOURCES})")
    entries, problems, seen = [], [], set()
    for i, r in enumerate(raw):
        name = _str(r.get("name") if isinstance(r, dict) else None, limit=100)
        label = name or f"#{i + 1}"
        if not name:
            problems.append({"name": label, "error": "no name"})
            continue
        if name in seen:
            problems.append({"name": name, "error": "the name appears twice in the file: only the first is used"})
            continue
        seen.add(name)
        scripts_raw = r.get("scripts") if isinstance(r.get("scripts"), list) else []
        if len(scripts_raw) > MAX_SCRIPTS:
            problems.append({"name": name, "error": f"too many script versions (max {MAX_SCRIPTS})"})
            continue
        scripts, bad = [], None
        ordered = sorted((x for x in scripts_raw if isinstance(x, dict)),
                         key=lambda x: x["version"] if isinstance(x.get("version"), int) else 0)
        for x in ordered:
            code = x.get("code")
            if not isinstance(code, str) or not code.strip():
                bad = "a script version has no code"
                break
            try:
                validate(code)
            except ValueError as e:
                bad = f"script v{x.get('version', '?')}: {e}"
                break
            scripts.append({"note": _str(x.get("note"), limit=200), "active": _bool(x.get("active")), "code": code})
        if bad:
            problems.append({"name": name, "error": bad})
            continue
        if scripts:                                    # exactly one active version: the last one marked, else the newest
            marked = [k for k, x in enumerate(scripts) if x["active"]]
            keep = marked[-1] if marked else len(scripts) - 1
            for k, x in enumerate(scripts):
                x["active"] = k == keep
        entries.append({
            "name": name, "public_name": _str(r.get("public_name"), limit=100), "base_url": _str(r.get("base_url")),
            "language": _str(r.get("language"), "en", 20) or "en", "enabled": _bool(r.get("enabled"), True),
            "starred": _bool(r.get("starred")), "rank": _int(r.get("rank"), 1, 9999, None),
            "timeout_s": _int(r.get("timeout_s"), 1, 300, 30),
            "uses_browser": _bool(r.get("uses_browser")), "browser_manual": _bool(r.get("browser_manual")),
            "scripts": scripts})
    return entries, problems


def free_name(name: str, taken: set[str]) -> str:
    base = name[:80]
    cand, n = f"{base} (imported)", 2
    while cand in taken:
        cand, n = f"{base} (imported {n})", n + 1
    return cand


def plan(entries: list[dict], existing: dict[str, set[str]], mode: str) -> list[dict]:
    """existing = {source name: {hash of each script it already has}}. mode: skip | update | copy (what to do when the name exists).
    Returns one action per entry: create / update / skip / copy, with the scripts that really are new."""
    taken, out = set(existing), []
    for e in entries:
        name, n = e["name"], len(e["scripts"])
        if name not in existing:
            taken.add(name)
            out.append({"entry": e, "action": "create", "final_name": name, "new_scripts": e["scripts"],
                        "detail": f"new source, {n} script version(s)"})
        elif mode == "skip":
            out.append({"entry": e, "action": "skip", "final_name": name, "new_scripts": [], "detail": "already exists: left untouched"})
        elif mode == "copy":
            final = free_name(name, taken)
            taken.add(final)
            out.append({"entry": e, "action": "copy", "final_name": final, "new_scripts": e["scripts"],
                        "detail": f"imported as a copy named “{final}”"})
        else:                                          # update
            have, new = set(existing[name]), []
            for s in e["scripts"]:
                h = code_hash(s["code"])
                if h not in have:
                    have.add(h)
                    new.append(s)
            out.append({"entry": e, "action": "update", "final_name": name, "new_scripts": new,
                        "detail": "settings updated, " + (f"{len(new)} new script version(s) added" if new else "its scripts are already there")})
    return out
