from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ResolveRequest(BaseModel):
    tmdb_id: int = Field(gt=0)
    type: Literal["movie", "tv"]
    season: int | None = Field(default=None, ge=0)
    episode: int | None = Field(default=None, ge=1)
    starred_first: bool = Field(default=False, description="Start the starred (reliable) sources before the others. "
                                "Matters when sources have to wait for a slot: the starred ones get it first")
    max_sources: int | None = Field(default=None, ge=1, le=50, description="Stop once this many sources found streams. "
                                    "Sources are tried in the admin's rank order (1 first). 1 = only the best source; ask for more later with "
                                    "POST /v1/tasks/{id}/sources. Omit = ask every source")

    @model_validator(mode="after")
    def _check(self):
        if self.type == "tv" and (self.season is None or self.episode is None):
            raise ValueError("season and episode are required for type 'tv'")
        if self.type == "movie":
            self.season = self.episode = None
        return self


class MoreSourcesRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=50, description="How many MORE sources to find for this task (next ones in rank order)")


class RefreshRequest(BaseModel):
    sources: list[str] = Field(default_factory=list, max_length=50, description="Sources to look up again: the names in the task's `sources[].source` (case-insensitive)")
    expired: bool = Field(default=False, description="Also every source whose links have all stopped working")
    failed: bool = Field(default=False, description="Also every source whose run failed")


class LiveRequest(BaseModel):
    stream_id: str


class LiveOut(BaseModel):
    id: str
    task_id: str
    stream_id: str
    source: str
    quality: str | None
    format: Literal["hls", "file"]          # hls: stream_url is a playlist; file: a plain video file (supports Range)
    stream_url: str                         # play this. Signed; nothing is saved on the server
    stream_url_expires_at: datetime         # when it stops working (never later than the source's own link): refresh before that
    reused: bool                            # true = the same stream was already live; you got the same session with a fresh link


class StreamOut(BaseModel):
    id: str
    quality: str | None
    format: str | None
    url: str
    size: int | None
    headers: dict | None
    expires_at: datetime | None = None   # when THIS link stops working (read from its token); null = unknown, it lives as long as the task


class SourceResultOut(BaseModel):
    source: str
    starred: bool = False       # true = a source you marked as reliable
    status: str
    error: str | None
    duration_ms: int | None
    streams: list[StreamOut]
    subtitles: list[dict]
    audio: list[dict]


class TaskOut(BaseModel):
    task_id: str
    status: str
    tmdb_id: int
    type: str
    season: int | None
    episode: int | None
    title: str | None
    created_at: datetime
    expires_at: datetime
    version: int = 0           # grows every time a source finishes; pass it back as ?after=
    sources_total: int = 0
    sources_done: int = 0
    sources_wanted: int | None = None   # the target set by max_sources / POST .../sources (null = all)
    sources_found: int = 0              # how many sources returned streams so far
    has_more: bool = False              # finished, but sources were never asked: POST /v1/tasks/{id}/sources finds more
    queue_position: int | None = None   # status "queued": 1 = next to start. Otherwise null
    sources: list[SourceResultOut]      # empty once status is "expired" (the stream links are gone)
    library: list["LibraryItemOut"] = []   # this task's library items (video downloads), newest first


class TaskSummary(BaseModel):
    """One line per task for GET /v1/tasks (no results)."""
    task_id: str
    status: str                # queued | pending | running | done | failed | expired
    tmdb_id: int
    type: str
    season: int | None
    episode: int | None
    title: str | None
    created_at: datetime
    expires_at: datetime       # when the RESULTS expire (the task itself is kept much longer)
    version: int = 0
    sources_total: int = 0
    sources_done: int = 0
    queue_position: int | None = None   # status "queued": 1 = next to start


# ---- admin ----
class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    public_name: str = Field(default="", max_length=100)   # shown to API clients instead of `name`
    starred: bool = False
    rank: int | None = Field(default=None, ge=1, le=9999)   # 1 = tried first (empty = last)
    base_url: str = ""
    language: str = "en"
    enabled: bool = True
    timeout_s: int = Field(default=30, ge=1, le=300)
    uses_browser: bool | None = None       # set by hand (otherwise detected from the script)


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    public_name: str | None = Field(default=None, max_length=100)
    starred: bool | None = None
    rank: int | None = Field(default=None, ge=1, le=9999)   # send null to clear the rank
    base_url: str | None = None
    language: str | None = None
    enabled: bool | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=300)
    uses_browser: bool | None = None       # setting this = "I decided": auto-detection stops touching it
    browser_auto: bool | None = None       # true = forget my choice, detect from the active script again


class ScriptIn(BaseModel):
    code: str
    note: str = ""
    activate: bool = True


class TestIn(BaseModel):
    tmdb_id: int = Field(gt=0)
    type: Literal["movie", "tv"]
    season: int | None = None
    episode: int | None = None
    version: int | None = None  # default: active version
    verbose: bool = False       # also log every Playwright/patchright call (DEBUG=pw:api) - very detailed


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    rate_limit_per_min: int = Field(default=60, ge=1)


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    rate_limit_per_min: int | None = Field(default=None, ge=1)


# ---- library ----
class SubtitleOut(BaseModel):
    lang: str                       # short code: en, fa, es ... ("und" = unknown)
    label: str                      # readable and unique within the item: "English", "English 2"
    format: str = "vtt"
    url: str                        # signed link on our own domain
    default: bool = False
    source: str | None = None       # public name of the provider (only for task-wide subtitles)


class LibraryRequest(BaseModel):
    stream_id: str
    ttl_hours: int | None = Field(default=None, ge=1, description="Auto-delete this many hours after the download finishes")
    quality: str | None = Field(default=None, description="HLS streams only: pick this quality (e.g. '720p'); default is the best")
    subtitles: bool | None = Field(default=None, description="Fetch this source's subtitles too (default: the server setting)")
    subtitle_langs: list[str] | None = Field(default=None, description="Only these languages, e.g. ['en','fa'] ([] = all)")
    subtitle_default: str | None = Field(default=None, description="Mark this language's first track as default, e.g. 'en'")
    progressive: bool = Field(default=False, description="true = download as HLS (playlist + segments) and give a stream_url "
                                                        "that is playable while the download is still running")


class LibraryItemOut(BaseModel):
    id: str
    task_id: str
    stream_id: str
    source: str
    quality: str | None
    format: str | None
    status: str
    error: str | None
    progress: float | None
    size: int | None
    total_bytes: int | None
    duration_s: float | None = None     # video length in seconds (null until known)
    duration: str | None = None         # same, formatted: "1:29:30" or "42:05"
    error_code: str | None = None       # "source_link_expired" = the source refused the link (resolve again / pick another stream)
    ttl_hours: int
    requested_at: datetime
    ready_at: datetime | None
    delete_at: datetime | None
    url: str | None            # signed link to the file, only for mode "file" when status == "ready"
    url_expires_at: datetime | None
    mode: str = "file"                      # "file" or "hls" (progressive)
    playable: bool = False                  # file: ready. hls: enough segments to start playing
    stream_url: str | None = None           # hls: signed playlist URL, available as soon as `playable`
    stream_url_expires_at: datetime | None = None
    subtitles: list[SubtitleOut] = []       # WebVTT tracks; grows while subtitles_status is "pending"
    subtitles_status: str = "none"          # pending | done | partial | none
    tmdb_id: int | None = None              # which movie/episode this is (copied from the task)
    type: str | None = None
    season: int | None = None
    episode: int | None = None
    title: str | None = None
    download_speed: int | None = None       # bytes/sec (smoothed ~8s); null unless status == "downloading"
    eta_seconds: int | None = None          # null unless status == "downloading"


class TaskSubtitlesRequest(BaseModel):
    subtitle_langs: list[str] | None = Field(default=None, description="Only these languages, e.g. ['en','fa'] ([] or omitted = all)")
    subtitle_default: str | None = Field(default=None, description="Mark this language's first track as default")
    ttl_hours: int | None = Field(default=None, ge=1, description="How long the hosted subtitles live")


class TaskSubtitlesOut(BaseModel):
    id: str                                  # use DELETE /v1/library/{id} to remove them early
    task_id: str
    tmdb_id: int | None = None
    type: str | None = None
    season: int | None = None
    episode: int | None = None
    title: str | None = None
    status: str                              # pending | done | partial | none
    subtitles: list[SubtitleOut] = []        # from ALL sources of the task, each with its public `source` name
    ttl_hours: int
    delete_at: datetime | None = None


TaskOut.model_rebuild()
