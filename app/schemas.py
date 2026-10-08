from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ResolveRequest(BaseModel):
    tmdb_id: int = Field(gt=0)
    type: Literal["movie", "tv"]
    season: int | None = Field(default=None, ge=0)
    episode: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check(self):
        if self.type == "tv" and (self.season is None or self.episode is None):
            raise ValueError("season and episode are required for type 'tv'")
        if self.type == "movie":
            self.season = self.episode = None
        return self


class StreamOut(BaseModel):
    id: str
    quality: str | None
    format: str | None
    url: str
    size: int | None
    headers: dict | None


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
    sources: list[SourceResultOut]


# ---- admin ----
class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    public_name: str = Field(default="", max_length=100)   # shown to API clients instead of `name`
    starred: bool = False
    base_url: str = ""
    language: str = "en"
    enabled: bool = True
    timeout_s: int = Field(default=30, ge=1, le=300)


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    public_name: str | None = Field(default=None, max_length=100)
    starred: bool | None = None
    base_url: str | None = None
    language: str | None = None
    enabled: bool | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=300)


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


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    rate_limit_per_min: int = Field(default=60, ge=1)


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    rate_limit_per_min: int | None = Field(default=None, ge=1)


# ---- library ----
class LibraryRequest(BaseModel):
    stream_id: str
    ttl_hours: int | None = Field(default=None, ge=1, description="Auto-delete this many hours after the download finishes")
    quality: str | None = Field(default=None, description="HLS streams only: pick this quality (e.g. '720p'); default is the best")


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
    ttl_hours: int
    requested_at: datetime
    ready_at: datetime | None
    delete_at: datetime | None
    url: str | None            # signed link, only when status == "ready"
    url_expires_at: datetime | None
