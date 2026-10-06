"""Example source script: Internet Archive (feature_films collection).

Contract: define resolve(ctx) and return {"streams": [...], "subtitles": [...], "audio": [...]}.
ctx has: tmdb_id, type, title, original_title, alt_titles, year, imdb_id, season, episode, ...
Uses only the standard library, so the script venv needs no extra packages.
Only searches the feature_films collection (public-domain / freely shareable films).
NOTE: written without network access, so test it with POST /admin/sources/{id}/test first.
"""
import json
import urllib.parse
import urllib.request

UA = {"User-Agent": "candyresolver/0.1"}


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def resolve(ctx):
    if ctx.get("type") != "movie":
        return {"streams": []}

    names = [n for n in [ctx.get("title"), ctx.get("original_title")] if n]
    year = ctx.get("year")
    doc = None
    for name in dict.fromkeys(names):
        q = f'title:("{name}") AND mediatype:movies AND collection:feature_films'
        params = urllib.parse.urlencode(
            {"q": q, "fl[]": ["identifier", "title", "year"], "rows": 10, "output": "json"}, doseq=True)
        docs = get_json("https://archive.org/advancedsearch.php?" + params)["response"]["docs"]
        for d in docs:
            try:
                if year and d.get("year") and abs(int(d["year"]) - int(year)) > 1:
                    continue
            except (TypeError, ValueError):
                pass
            doc = d
            break
        if doc:
            break
    if not doc:
        return {"streams": []}

    ident = doc["identifier"]
    meta = get_json(f"https://archive.org/metadata/{ident}")
    base = f"https://archive.org/download/{ident}/"

    streams, subtitles = [], []
    for f in meta.get("files", []):
        name = f.get("name", "")
        low = name.lower()
        url = base + urllib.parse.quote(name)
        if low.endswith((".mp4", ".m4v")):
            height = f.get("height")
            streams.append({
                "url": url,
                "quality": f"{height}p" if height else None,
                "format": "mp4",
                "size": int(f["size"]) if str(f.get("size", "")).isdigit() else None,
                "extra": {"identifier": ident, "file": name},
            })
        elif low.endswith((".srt", ".vtt")):
            subtitles.append({"lang": f.get("language", "und"), "url": url,
                              "format": low.rsplit(".", 1)[-1]})
    return {"streams": streams, "subtitles": subtitles}
