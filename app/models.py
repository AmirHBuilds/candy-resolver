import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import settings
from .db import Base


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def public_label(name: str | None, source_id: int | None = None) -> str:
    """The name clients see for a provider. Never the real name; falls back to a neutral label."""
    n = (name or "").strip()
    if n:
        return n
    return f"Server {source_id}" if source_id else "Server"


def default_expiry() -> datetime:
    return utcnow() + timedelta(hours=settings.task_ttl_hours)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)          # real name, admin only
    public_name: Mapped[str] = mapped_column(String(100), default="")    # what API clients see
    base_url: Mapped[str] = mapped_column(String(500), default="")
    language: Mapped[str] = mapped_column(String(20), default="en")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    starred: Mapped[bool] = mapped_column(Boolean, default=False)       # trusted / reliable source
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)    # 1 = tried first, 2 = second... (null = last)
    timeout_s: Mapped[int] = mapped_column(Integer, default=30)
    # Heavy script (drives a real browser, ~400 MB): counted against MAX_BROWSER_SCRIPTS.
    # Detected from the script's imports unless the admin set it by hand (browser_manual).
    uses_browser: Mapped[bool] = mapped_column(Boolean, default=False)
    browser_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    scripts: Mapped[list["Script"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", order_by="Script.version"
    )


class Script(Base):
    """One uploaded version of a source's script. Exactly one version per source is active."""
    __tablename__ = "scripts"
    __table_args__ = (UniqueConstraint("source_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    version: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    source: Mapped[Source] = relationship(back_populates="scripts")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(12))
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("tsk"))
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    tmdb_id: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(10))
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    starred_first: Mapped[bool] = mapped_column(Boolean, default=False)   # request option: starred sources start first
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # TMDB info handed to scripts
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|done|failed
    version: Mapped[int] = mapped_column(Integer, default=0)        # +1 every time a source finishes
    sources_total: Mapped[int] = mapped_column(Integer, default=0)  # how many sources this task runs
    starred_total: Mapped[int] = mapped_column(Integer, default=0)  # how many of them are starred
    sources_wanted: Mapped[int | None] = mapped_column(Integer, nullable=True)   # stop once this many sources found streams (null = ask all)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=default_expiry)

    runs: Mapped[list["SourceRun"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="SourceRun.id"
    )


class SourceRun(Base):
    """Result of running one source's script for one task."""
    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # no FK: history survives source deletion
    source_name: Mapped[str] = mapped_column(String(100))
    public_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    starred: Mapped[bool] = mapped_column(Boolean, default=False)       # snapshot of the source's star at run time
    script_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # ok | empty | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peak_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)   # peak RAM of the script and its children
    subtitles: Mapped[list] = mapped_column(JSON, default=list)
    audio: Mapped[list] = mapped_column(JSON, default=list)

    task: Mapped[Task] = relationship(back_populates="runs")
    streams: Mapped[list["Stream"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Stream.id"
    )


class Stream(Base):
    __tablename__ = "streams"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("str"))
    run_id: Mapped[int] = mapped_column(ForeignKey("source_runs.id"))
    quality: Mapped[str | None] = mapped_column(String(20), nullable=True)
    format: Mapped[str | None] = mapped_column(String(20), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # headers needed to fetch the url
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)   # when this link stops working (read from its token)
    dead: Mapped[bool] = mapped_column(Boolean, default=False)   # a download of it was refused by the source (401/403/404/410)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    run: Mapped[SourceRun] = relationship(back_populates="streams")


class LibraryItem(Base):
    """A stream downloaded to our own disk. Independent of tasks: it outlives task expiry."""
    __tablename__ = "library_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("lib"))
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    task_id: Mapped[str] = mapped_column(String(40))      # no FK on purpose
    stream_id: Mapped[str] = mapped_column(String(40), index=True)
    source_name: Mapped[str] = mapped_column(String(100))
    public_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quality: Mapped[str | None] = mapped_column(String(20), nullable=True)
    format: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # queued | downloading | ready | failed | expired | deleted
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)   # copied from the task: items describe
    media_type: Mapped[str | None] = mapped_column(String(10), nullable=True)          # themselves, even after the task's
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)                 # results are long gone
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    mode: Mapped[str] = mapped_column(String(10), default="file")        # file = single mp4/mkv, hls = playlist + segments, subs = subtitles of a whole task
    playable: Mapped[bool] = mapped_column(Boolean, default=False)       # hls: enough segments exist to start playing
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    subtitles: Mapped[list | None] = mapped_column(JSON, nullable=True)         # [{"n","lang","label","default"}], grows
    subtitles_status: Mapped[str] = mapped_column(String(12), default="none")    # pending | done | partial | none
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)         # bytes so far / final
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # from Content-Length
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)      # video length in seconds (ffprobe)
    ttl_hours: Mapped[int] = mapped_column(Integer, default=24)
    opts: Mapped[str | None] = mapped_column(String(160), nullable=True)   # request options that make two downloads "the same"
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
