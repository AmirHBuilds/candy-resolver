"""Live relay: play a source's stream through candyresolver without saving anything.

Why: source links are tied to the VPS's IP (and need special headers), so the player cannot fetch them itself. Candyresolver fetches
every playlist / segment / key from the source and hands the player ordinary links on our own domain.

How: a LIVE SESSION (in memory) belongs to one stream. Every upstream URL found in a playlist gets a number in the session's table and
is rewritten to "{n}.{ext}?exp=..&sig=..". Nobody can make us fetch an arbitrary URL: only numbers we handed out work, and addresses
that point into private networks are refused. Segments are streamed chunk by chunk (RAM per viewer stays at a few MB).

Pure logic: no database, no web framework, the HTTP client is injected, so everything here is tested with a fake source."""
import asyncio
import ipaddress
import json
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
TYPES = {"m3u8": PLAYLIST_TYPE, "ts": "video/mp2t", "m4s": "video/mp4", "mp4": "video/mp4", "mkv": "video/x-matroska",
         "webm": "video/webm", "mov": "video/quicktime", "m4v": "video/mp4", "mp3": "audio/mpeg", "aac": "audio/aac",
         "m4a": "audio/mp4", "vtt": "text/vtt", "key": "application/octet-stream", "bin": "application/octet-stream"}
SID = re.compile(r"liv_[0-9a-f]{16}")
NAME = re.compile(r"(\d{1,6})\.(" + "|".join(TYPES) + r")")
FILE_EXTS = ("mp4", "mkv", "webm", "mov", "m4v")
MAX_TABLE = 20000                 # upstream URLs one session may hold
MAX_REDIRECTS = 5
MAX_PLAYLIST = 5 * 1024 * 1024
DEFAULT_UA = "candyresolver/0.1"
_REDIRECTS = (301, 302, 303, 307, 308)
_RANGE = re.compile(r"bytes=\d*-\d*")


class RelayError(Exception):
    def __init__(self, code: str, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.status, self.message, self.retry_after = code, status, message, retry_after


class FetchError(Exception):
    """Network problem talking to the source (refused, reset, DNS...)."""


class FetchTimeout(FetchError):
    """The source did not answer in time."""


class Capacity(Exception):
    """Too many live streams at once."""


class TableFull(Exception):
    pass


@dataclass
class UpstreamResponse:
    status: int
    headers: dict                                   # lower-case names
    iter: AsyncIterator[bytes]
    aclose: Callable[[], Awaitable[None]]


# ---------------------------------------------------------------- what is behind a URL
def classify(url: str, hint: str | None = None) -> tuple[str, str]:
    """(kind, extension) of an upstream URL: playlist / segment / key / init. Unknown things (even .jpg or .html: some sources
    disguise their segments) are treated as .ts segments."""
    last = urlsplit(url).path.lower().rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1] if "." in last else ""
    if hint == "key":
        return "key", "key"
    if hint == "init":
        return "init", "mp4"
    if hint == "playlist" or ext in ("m3u8", "m3u"):
        return "playlist", "m3u8"
    if ext in ("vtt", "webvtt"):
        return "segment", "vtt"
    if ext in ("m4s", "cmfv", "cmfa", "mp4", "m4v"):
        return "segment", "m4s"
    if ext in ("aac", "mp3", "m4a"):
        return "segment", ext
    return "segment", "ts"


def file_ext(fmt: str | None, url: str) -> str:
    cand = (fmt or "").lower().lstrip(".")
    if cand not in FILE_EXTS:
        cand = urlsplit(url).path.lower().rsplit(".", 1)[-1]
    return cand if cand in FILE_EXTS else "mp4"


_URI_ATTR = re.compile(r'URI="([^"]*)"')
_TAG_HINT = {"#EXT-X-KEY": "key", "#EXT-X-SESSION-KEY": "key", "#EXT-X-MAP": "init",
             "#EXT-X-MEDIA": "playlist", "#EXT-X-I-FRAME-STREAM-INF": "playlist"}


def rewrite_playlist(text: str, base: str, register: Callable[[str, str, str], int], query: str) -> str:
    """Every URL in the playlist (variants, segments, keys, init segments, audio / subtitle renditions) becomes
    "{n}.{ext}?{query}", where n is the number register() gave that upstream URL. Everything else is kept as it is."""
    def ref(uri: str, hint: str | None) -> str:
        absolute = urljoin(base, uri)
        if urlsplit(absolute).scheme not in ("http", "https"):
            return uri                                           # skd://, data: ... nothing we can fetch
        kind, ext = classify(absolute, hint)
        return f"{register(absolute, kind, ext)}.{ext}?{query}"

    out, variant_next = [], False
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            out.append("")
            continue
        if s.startswith("#"):
            tag = s.split(":", 1)[0]
            hint = _TAG_HINT.get(tag)
            if hint and 'URI="' in s:
                s = _URI_ATTR.sub(lambda m: 'URI="' + ref(m.group(1), hint) + '"', s)
            variant_next = variant_next or tag == "#EXT-X-STREAM-INF"
            out.append(s)
            continue
        out.append(ref(s, "playlist" if variant_next else None))
        variant_next = False
    return "\n".join(out) + "\n"


def ts_offset(buf: bytes) -> int:
    """Some sources put junk (a fake image header) in front of each MPEG-TS segment. Returns how many bytes to cut, 0 = nothing to cut
    (a clean segment, or not MPEG-TS at all, e.g. fMP4: those are never touched)."""
    if len(buf) < 189 or (buf[0] == 0x47 and buf[188] == 0x47):
        return 0
    for off in range(1, len(buf) - 376):
        if buf[off] == 0x47 and buf[off + 188] == 0x47 and buf[off + 376] == 0x47:
            return off
    return 0


# ---------------------------------------------------------------- do not become an open proxy
_safe_cache: dict[str, tuple[float, bool]] = {}


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


async def url_is_safe(url: str, resolve=None, clock=time.monotonic) -> bool:
    """False for anything that is not a public http(s) address (localhost, private networks, cloud metadata...): playlists come from the
    internet, and the relay returns what it fetches."""
    try:
        p = urlsplit(url)
        host = p.hostname
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not host:
        return False
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_global
    except ValueError:
        pass
    hit = _safe_cache.get(host)
    if hit and hit[0] > clock():
        return hit[1]
    try:
        ips = await (resolve or _resolve)(host)
    except Exception:
        return True                                              # cannot resolve: the fetch will fail on its own, as an ordinary network error
    ok = bool(ips) and all(ipaddress.ip_address(i.split("%")[0]).is_global for i in ips)
    if len(_safe_cache) > 2000:
        _safe_cache.clear()
    _safe_cache[host] = (clock() + 60, ok)
    return ok


# ---------------------------------------------------------------- sessions
@dataclass
class LiveSession:
    id: str
    api_key_id: int
    task_id: str
    stream_id: str
    source: str
    quality: str | None
    kind: str                                       # "hls" or "file"
    headers: dict
    ua: str
    created: float
    expires: float
    last_seen: float
    table: list = field(default_factory=list)       # n -> (upstream url, kind, ext)
    index: dict = field(default_factory=dict)       # upstream url -> n
    bytes_out: int = 0
    requests: int = 0
    errors: dict = field(default_factory=dict)
    dead: bool = False                              # the source refused the link: every request now answers "link expired" at once


class LiveRegistry:
    def __init__(self, max_active: int = 10, max_sessions: int = 200, active_window: float = 120, clock=time.time):
        self.max_active, self.max_sessions, self.active_window, self.clock = max_active, max_sessions, active_window, clock
        self.sessions: dict[str, LiveSession] = {}
        self._by_stream: dict[tuple[int, str], str] = {}
        self.totals = {"started": 0, "bytes": 0, "errors": 0}

    def _drop(self, sid: str) -> None:
        s = self.sessions.pop(sid, None)
        if s and self._by_stream.get((s.api_key_id, s.stream_id)) == sid:
            del self._by_stream[(s.api_key_id, s.stream_id)]

    def _gc(self, now: float) -> None:
        for sid in [k for k, s in self.sessions.items() if now >= s.expires]:
            self._drop(sid)

    def is_active(self, s: LiveSession, now: float) -> bool:
        return not s.dead and now < s.expires and now - s.last_seen < self.active_window

    def active_count(self) -> int:
        now = self.clock()
        return sum(1 for s in self.sessions.values() if self.is_active(s, now))

    def start(self, *, api_key_id: int, task_id: str, stream_id: str, source: str, quality: str | None, kind: str, entry_url: str,
              entry_ext: str, headers: dict, ua: str, ttl_s: int) -> tuple[LiveSession, bool]:
        """(session, reused). The same stream of the same key shares one session. Raises Capacity when too many streams are being watched."""
        now = self.clock()
        self._gc(now)
        old = self.sessions.get(self._by_stream.get((api_key_id, stream_id), ""))
        if old and not old.dead:
            old.expires = max(old.expires, now + ttl_s)
            old.last_seen = now
            return old, True
        if old:
            self._drop(old.id)
        if self.active_count() >= self.max_active:
            raise Capacity()
        if len(self.sessions) >= self.max_sessions:               # keep memory bounded: forget the least recently used
            self._drop(min(self.sessions.values(), key=lambda s: s.last_seen).id)
        s = LiveSession(id="liv_" + secrets.token_hex(8), api_key_id=api_key_id, task_id=task_id, stream_id=stream_id, source=source,
                        quality=quality, kind=kind, headers=headers, ua=ua, created=now, expires=now + ttl_s, last_seen=now)
        s.table.append((entry_url, "playlist" if kind == "hls" else "file", entry_ext))      # number 0 is always the entry point
        s.index[entry_url] = 0
        self.sessions[s.id] = s
        self._by_stream[(api_key_id, stream_id)] = s.id
        self.totals["started"] += 1
        return s, False

    def get(self, sid: str) -> LiveSession | None:
        s = self.sessions.get(sid)
        if s and self.clock() >= s.expires:
            self._drop(sid)
            return None
        return s

    def end(self, sid: str, api_key_id: int | None = None) -> bool:
        s = self.sessions.get(sid)
        if not s or (api_key_id is not None and s.api_key_id != api_key_id):
            return False
        self._drop(sid)
        return True

    def register(self, s: LiveSession, url: str, kind: str, ext: str) -> int:
        n = s.index.get(url)
        if n is None:
            if len(s.table) >= MAX_TABLE:
                raise TableFull()
            n = len(s.table)
            s.table.append((url, kind, ext))
            s.index[url] = n
        return n

    def count(self, s: LiveSession, nbytes: int) -> None:
        s.bytes_out += nbytes
        self.totals["bytes"] += nbytes

    def note_error(self, s: LiveSession, code: str) -> None:
        s.errors[code] = s.errors.get(code, 0) + 1
        self.totals["errors"] += 1

    def snapshot(self) -> dict:
        now = self.clock()
        self._gc(now)
        rows = [{"id": s.id, "task_id": s.task_id, "stream_id": s.stream_id, "source": s.source, "quality": s.quality, "kind": s.kind,
                 "created": s.created, "last_seen": s.last_seen, "expires": s.expires, "active": self.is_active(s, now), "dead": s.dead,
                 "bytes_out": s.bytes_out, "requests": s.requests, "errors": dict(s.errors), "entries": len(s.table)}
                for s in sorted(self.sessions.values(), key=lambda x: -x.last_seen)]
        return {"max_active": self.max_active, "active": sum(1 for r in rows if r["active"]), "sessions": rows, "totals": dict(self.totals)}


# ---------------------------------------------------------------- serving one request
@dataclass
class LiveReply:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    stream: AsyncIterator[bytes] | None = None
    close: Callable[[], Awaitable[None]] | None = None
    error: RelayError | None = None
    mark_dead: bool = False                          # the caller should flag the stream as dead in the database (first time only)


def _error_reply(e: RelayError, cors: dict, mark_dead: bool = False) -> LiveReply:
    h = {**cors, "Content-Type": "application/json", "Cache-Control": "no-store", "X-Candy-Error": e.code}
    if e.retry_after:
        h["Retry-After"] = str(e.retry_after)
    return LiveReply(e.status, h, body=json.dumps({"code": e.code, "message": e.message}).encode(), error=e, mark_dead=mark_dead)


_EXPIRED = RelayError("source_link_expired", 410, "the source stopped accepting this link: refresh the source (POST /v1/tasks/{id}/refresh) and start the stream again")


def _status_error(status: int, kind: str) -> RelayError | None:
    if status in (401, 403, 410):
        return _EXPIRED
    if status == 404:
        return _EXPIRED if kind == "playlist" else RelayError("upstream_not_found", 404, "the source does not have this part (404)")
    if status == 429:
        return RelayError("upstream_rate_limited", 503, "the source is limiting requests: try again in a few seconds", retry_after=5)
    if status >= 400 and status != 416:
        return RelayError("upstream_error", 502, f"the source answered HTTP {status}")
    if status < 200 or 300 <= status < 400:
        return RelayError("upstream_error", 502, f"unexpected answer from the source (HTTP {status})")
    return None


async def _get(fetcher, url: str, headers: dict, attempts: int, sleep) -> UpstreamResponse:
    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = await fetcher.get(url, headers)
        except FetchTimeout as e:
            last = e
        except FetchError as e:
            last = e
        else:
            if resp.status >= 500 and i < attempts - 1:          # a hiccup on the source: once more
                await resp.aclose()
                await sleep(0.4 * (i + 1))
                continue
            return resp
        if i < attempts - 1:
            await sleep(0.4 * (i + 1))
    if isinstance(last, FetchTimeout):
        raise RelayError("upstream_timeout", 504, "the source did not answer in time")
    raise RelayError("upstream_error", 502, "could not reach the source")


async def _open(fetcher, url: str, headers: dict, attempts: int, sleep, safe) -> tuple[UpstreamResponse, str]:
    """GET with redirects done by us, so that every hop is checked and the final URL is known (relative links resolve against it)."""
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        if not await safe(cur):
            raise RelayError("upstream_blocked", 502, "the address is not a public internet address: refused")
        resp = await _get(fetcher, cur, headers, attempts, sleep)
        if resp.status in _REDIRECTS:
            loc = resp.headers.get("location")
            await resp.aclose()
            if not loc:
                raise RelayError("upstream_error", 502, "the source sent an empty redirect")
            cur = urljoin(cur, loc)
            continue
        return resp, cur
    raise RelayError("upstream_error", 502, "too many redirects")


async def _read_capped(resp: UpstreamResponse, limit: int) -> bytes:
    buf = bytearray()
    try:
        async for chunk in resp.iter:
            buf += chunk
            if len(buf) > limit:
                raise RelayError("upstream_bad_playlist", 502, "the playlist is too large")
    except FetchTimeout:
        raise RelayError("upstream_timeout", 504, "the source stopped answering")
    except FetchError:
        raise RelayError("upstream_error", 502, "the source cut the connection")
    finally:
        await resp.aclose()
    return bytes(buf)


async def serve(reg: LiveRegistry, sess: LiveSession, fetcher, name: str, query: str, method: str, range_header: str | None,
                cors: dict, *, safe=url_is_safe, sleep=asyncio.sleep, max_playlist: int = MAX_PLAYLIST) -> LiveReply:
    """Answer GET/HEAD /l/{session}/{name}. `query` is the "exp=..&sig=.." the player used (put on every rewritten link)."""
    sess.last_seen = reg.clock()
    sess.requests += 1
    try:
        if sess.dead:
            return _error_reply(_EXPIRED, cors)
        m = NAME.fullmatch(name or "")
        n = int(m.group(1)) if m else -1
        if not m or n >= len(sess.table) or sess.table[n][2] != m.group(2):
            raise RelayError("live_ref_unknown", 404, "unknown part of this stream")
        url, kind, ext = sess.table[n]
        headers = {**sess.headers, "User-Agent": sess.ua, "Accept-Encoding": "identity"}
        if kind == "playlist":
            return await _playlist(reg, sess, fetcher, url, query, method, cors, headers, safe, sleep, max_playlist)
        return await _media(reg, sess, fetcher, url, kind, ext, method, range_header, cors, headers, safe, sleep)
    except RelayError as e:
        reg.note_error(sess, e.code)
        first = e is _EXPIRED and not sess.dead
        if e is _EXPIRED:
            sess.dead = True
        return _error_reply(e, cors, mark_dead=first)


async def _playlist(reg, sess, fetcher, url, query, method, cors, headers, safe, sleep, max_playlist) -> LiveReply:
    resp, final = await _open(fetcher, url, headers, 2, sleep, safe)
    err = _status_error(resp.status, "playlist")
    if err:
        await resp.aclose()
        raise err
    data = await _read_capped(resp, max_playlist)
    text = data.decode("utf-8", "replace").lstrip("\ufeff \t\r\n")
    if not text.startswith("#EXTM3U"):
        raise RelayError("upstream_bad_playlist", 502, "the source did not return a playlist (blocked, or the link expired?)")
    try:
        body = rewrite_playlist(text, final, lambda u, k, e: reg.register(sess, u, k, e), query).encode()
    except TableFull:
        raise RelayError("upstream_bad_playlist", 502, "the playlist has too many entries")
    h = {**cors, "Content-Type": PLAYLIST_TYPE, "Content-Length": str(len(body)), "Cache-Control": "no-store"}
    return LiveReply(200, h, body=None if method == "HEAD" else body)


async def _media(reg, sess, fetcher, url, kind, ext, method, range_header, cors, headers, safe, sleep) -> LiveReply:
    rng = range_header if range_header and len(range_header) < 100 and _RANGE.fullmatch(range_header) else None
    if rng:
        headers = {**headers, "Range": rng}
    resp, _ = await _open(fetcher, url, headers, 2, sleep, safe)
    h = {**cors, "Content-Type": TYPES[ext], "Cache-Control": "no-store" if kind == "key" else "private, max-age=3600"}
    for up, ours in (("content-range", "Content-Range"), ("accept-ranges", "Accept-Ranges")):
        if resp.headers.get(up):
            h[ours] = resp.headers[up]
    if resp.status == 416:                                     # the player asked for bytes the file does not have
        await resp.aclose()
        return LiveReply(416, h)
    err = _status_error(resp.status, kind)
    if err:
        await resp.aclose()
        raise err
    length = resp.headers.get("content-length")
    first = b""
    cut = 0
    if ext == "ts" and not rng and resp.status == 200:         # look at the start of the segment: junk in front?
        try:
            while len(first) < 4096:
                first += await resp.iter.__anext__()
        except StopAsyncIteration:
            pass
        except FetchError:
            await resp.aclose()
            raise RelayError("upstream_error", 502, "the source cut the connection")
        cut = ts_offset(first)
        first = first[cut:]
    if length and length.isdigit():
        h["Content-Length"] = str(max(int(length) - cut, 0))
    if method == "HEAD":
        await resp.aclose()
        return LiveReply(resp.status, h)

    async def body():
        try:
            if first:
                reg.count(sess, len(first))
                yield first
            async for chunk in resp.iter:
                reg.count(sess, len(chunk))
                yield chunk
        finally:
            await resp.aclose()

    return LiveReply(resp.status, h, stream=body(), close=resp.aclose)
