"""Fetch + convert + store the subtitles of one library item, in parallel with its video.
Nothing in here may ever raise into the caller or touch the video: every problem just means "that subtitle is missing"."""
import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .runner import _kill
from .subfetch import fetch_blocking, is_public_ip
from .subtitles import SubtitleRejected, lang_label, normalize_lang, sniff, to_vtt

MB = 1024 * 1024
_ffmpeg_sem: asyncio.Semaphore | None = None


@dataclass
class Params:
    max_files: int = 30
    max_bytes: int = 2 * MB
    timeout_s: float = 15
    ffmpeg_fallback: bool = True
    ffmpeg: str = "ffmpeg"
    default_lang: str | None = None             # normalized code; the first finished track of it gets default=true
    policy: Callable[[str], bool] = is_public_ip
    sem: asyncio.Semaphore | None = None        # shared limit on simultaneous downloads (all items)


@dataclass
class Candidate:
    n: int                                      # file name subs/<n>.vtt; fixed by source order, so labels never shift
    lang: str
    label: str
    url: str
    headers: dict | None = None
    source: str | None = None                   # public provider name (task-wide subtitles only)


@dataclass
class Plan:
    candidates: list[Candidate] = field(default_factory=list)
    over_cap: int = 0                           # candidates dropped because of max_files


@dataclass
class Result:
    status: str                                 # done | partial | none
    tracks: list[dict]
    failures: list[tuple[int, str]]


def parse_langs(value) -> list[str]:
    """'en, fa' or ['en','Persian'] -> ['en','fa'] (normalized, no unknowns, no duplicates)."""
    items = value.split(",") if isinstance(value, str) else (value or [])
    out = []
    for v in items:
        c = normalize_lang(v)
        if c != "und" and c not in out:
            out.append(c)
    return out


def plan_subtitles(raw, *, allow_langs: list[str] | None = None, max_files: int = 30) -> Plan:
    """Which of the source's subtitles to fetch: valid http(s) URLs, no duplicates, language allow-list applied
    (empty/None = all languages), capped, numbered in source order, with unique labels per language."""
    allow = set(allow_langs or [])
    seen, picked = set(), []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        url = d.get("url")
        if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
            continue
        url = url.strip()
        lang = normalize_lang(d.get("lang") or d.get("language") or d.get("srclang") or d.get("label"))
        if (lang, url) in seen:
            continue
        seen.add((lang, url))
        if allow and lang not in allow:
            continue
        picked.append((lang, url, d.get("headers") if isinstance(d.get("headers"), dict) else None,
                       d.get("_source") if isinstance(d.get("_source"), str) else None))
    over = max(0, len(picked) - max_files)
    picked = picked[:max_files]
    totals, count, cands = {}, {}, []
    for lang, _, _, _ in picked:
        totals[lang] = totals.get(lang, 0) + 1
    for i, (lang, url, headers, source) in enumerate(picked, 1):
        count[lang] = count.get(lang, 0) + 1
        base = lang_label(lang)
        label = base if totals[lang] == 1 or count[lang] == 1 else f"{base} {count[lang]}"
        cands.append(Candidate(i, lang, label, url, headers, source))
    return Plan(cands, over)


def write_atomic(item_dir: Path, n: int, vtt: str) -> None:
    """subs/<n>.vtt, written to a temp name first so a half-written file is never served.
    It never creates the item folder: if the item was deleted/failed meanwhile, nothing is re-created."""
    if not item_dir.is_dir():
        raise FileNotFoundError("item folder is gone")
    subs = item_dir / "subs"
    try:
        subs.mkdir()                                  # not parents=True, on purpose
    except FileExistsError:
        pass
    tmp = subs / f"{n}.vtt.tmp"
    tmp.write_text(vtt, encoding="utf-8")
    os.replace(tmp, subs / f"{n}.vtt")


async def _ass_with_ffmpeg(text: str, params: Params) -> tuple[str, int]:
    """ASS/SSA is odd enough to need ffmpeg: one at a time, tiny input, hard timeout."""
    global _ffmpeg_sem
    if _ffmpeg_sem is None:
        _ffmpeg_sem = asyncio.Semaphore(1)
    async with _ffmpeg_sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                params.ffmpeg, "-v", "error", "-f", "ass", "-i", "pipe:0", "-f", "webvtt", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except FileNotFoundError:
            raise SubtitleRejected("ffmpeg not installed")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(text.encode("utf-8")), 20)
        except asyncio.TimeoutError:
            raise SubtitleRejected("conversion timed out")
        finally:
            if proc.returncode is None:
                _kill(proc)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except Exception:
                    pass
    if proc.returncode != 0 or not out:
        raise SubtitleRejected("unsupported ASS/SSA file")
    return to_vtt("vtt", out.decode("utf-8", "replace"))


async def run_subtitles(plan: Plan, item_dir, params: Params, default_headers: dict | None,
                        on_progress: Callable[[list[dict], str], Awaitable[None]]) -> Result:
    """Runs all candidates concurrently (limited by params.sem). Calls on_progress(tracks, "pending") each time
    one more file is ready, and once more with the final status. Never raises (except cancellation)."""
    item_dir = Path(item_dir)
    sem = params.sem or asyncio.Semaphore(4)
    tracks: list[dict] = []
    failures: list[tuple[int, str]] = []
    seen: set[tuple[str, str]] = set()
    dupes = 0

    async def emit(status: str) -> None:
        try:
            await on_progress(sorted((dict(t) for t in tracks), key=lambda t: t["n"]), status)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass                                       # a bookkeeping problem must not stop the other subtitles

    async def one(c: Candidate) -> None:
        nonlocal dupes
        try:
            async with sem:
                hdrs = c.headers if c.headers else default_headers
                raw = await asyncio.wait_for(
                    asyncio.to_thread(fetch_blocking, c.url, hdrs, max_bytes=params.max_bytes,
                                      timeout_s=params.timeout_s, policy=params.policy),
                    params.timeout_s + 3)
            kind, text, _enc = sniff(raw, c.lang)       # detect from the content, then convert
            if kind == "ass":
                if not params.ffmpeg_fallback:
                    raise SubtitleRejected("ASS/SSA not supported (fallback disabled)")
                vtt, _cues = await _ass_with_ffmpeg(text, params)
            else:
                vtt, _cues = to_vtt(kind, text)
            key = (c.lang, hashlib.sha1(vtt.encode("utf-8")).hexdigest())
            if key in seen:                              # same language + same content under another URL
                dupes += 1
                return
            seen.add(key)
            await asyncio.to_thread(write_atomic, item_dir, c.n, vtt)
            tracks.append({"n": c.n, "lang": c.lang, "label": c.label, "source": c.source,
                           "default": bool(params.default_lang and c.lang == params.default_lang
                                           and not any(t["default"] for t in tracks))})
            await emit("pending")
        except asyncio.CancelledError:
            raise
        except Exception as e:                           # any failure = this subtitle is missing, nothing else
            failures.append((c.n, str(e) or e.__class__.__name__))

    try:
        await asyncio.gather(*(one(c) for c in plan.candidates))
    except asyncio.CancelledError:
        raise
    attempted = len(plan.candidates) + plan.over_cap
    ok = len(tracks)
    if ok == 0:
        status = "none"
    elif ok + dupes == attempted:
        status = "done"
    else:
        status = "partial"
    await emit(status)
    return Result(status, sorted(tracks, key=lambda t: t["n"]), sorted(failures))
