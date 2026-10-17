import json
import os
import secrets
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./candyresolver.db"
    tmdb_api_key: str = ""

    # Left empty -> generated on first start and kept in DATA_DIR/secrets.json
    admin_token: str = ""
    signing_secret: str = ""

    # Used to build absolute file links, e.g. https://candy.example.com (default: taken from the request)
    public_base_url: str = ""
    data_dir: str = "./data"

    # script runner
    script_python: str = sys.executable
    # 0 = no RLIMIT_AS. An address-space limit breaks Chromium/Node (they reserve huge virtual ranges and
    # die with "Connection closed while reading from the driver"). Cap memory with the container limit instead.
    script_memory_mb: int = 0
    script_cpu_seconds: int = 300          # CPU seconds (not wall time); a real browser burns a lot
    script_max_file_mb: int = 500          # biggest single file a script may write (browser cache / recordings)
    script_max_open_files: int = 4096
    playwright_browsers_path: str = "/ms-playwright"
    script_max_output_bytes: int = 1_000_000
    script_run_as_uid: int | None = None
    script_run_as_gid: int | None = None

    # tasks: the record is kept for a long time; only its RESULTS (stream links) expire
    task_ttl_hours: int = 24               # how long the results stay valid; then status becomes "expired"
    task_retention_days: int = 365         # how long the task record itself is kept (0 = forever)
    max_parallel_sources: int = 8
    # Memory protection (a browser-based source costs ~400 MB; a small server cannot run many at once)
    max_concurrent_tasks: int = 2          # tasks running at the same time; the rest wait in line. 0 = unlimited
    max_queued_tasks: int = 20             # waiting tasks beyond this get HTTP 429 (Retry-After)
    max_browser_scripts: int = 1           # browser-based source scripts running at once, server-wide. 0 = unlimited

    # library
    library_ttl_hours: int = 24          # default auto-delete time after a download finishes
    library_max_ttl_hours: int = 168     # longest a client may ask for
    library_concurrency: int = 2         # simultaneous downloads
    telegram_bot_token: str = ""        # optional first-time defaults for Telegram alerts; afterwards edit them in the admin panel (Telegram page)
    telegram_chat_id: str = ""
    server_name: str = ""                # shown in Telegram messages (default: the container's hostname)
    hostclean_dir: str = "/hostclean"   # folder shared with the host agent (scripts/host_cleanup.sh --install-agent); see docker-compose.yml
    live_enabled: bool = True              # the live relay (POST /v1/tasks/{id}/live): play a source's stream through us, nothing saved
    live_max_sessions: int = 10            # streams being watched at the same time (each costs the VPS the stream's bandwidth)
    audio_concurrency: int = 2           # simultaneous GET /v1/library/{id}/audio extractions (each runs one ffmpeg); more get HTTP 429
    library_max_size_gb: int = 20        # refuse bigger files
    library_min_free_gb: int = 2         # keep this much disk free
    signed_url_ttl_min: int = 360        # lifetime of each signed file link
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # progressive HLS (play while downloading)
    progressive_segment_seconds: int = 4   # length of each .ts segment (cut at keyframes, so can be longer)
    progressive_min_segments: int = 3      # `playable` once this many segments are finished (~12s of video)
    progressive_audio_bitrate: str = "128k"  # only used when audio has to be converted to aac
    hls_link_ttl_min: int = 720            # lifetime of a stream_url: long enough for a whole movie
    # subtitles (downloaded next to the video, converted to WebVTT, served from our own signed links)
    subtitles_enabled: bool = True         # global default; a library request can override with "subtitles": true/false
    subtitle_max_files: int = 30           # per item
    subtitle_max_bytes: int = 2 * 1024 * 1024   # per file, enforced while downloading
    subtitle_timeout_s: int = 15           # per file
    subtitle_concurrency: int = 4          # simultaneous subtitle downloads, server-wide
    subtitle_langs: str = ""               # allow-list, e.g. "en,fa" (empty = every language); request "subtitle_langs" overrides
    subtitle_default_lang: str = ""        # mark this language's first track default (empty = never mark a default)
    subtitle_pack_ttl_hours: int = 168     # POST /v1/tasks/{id}/subtitles: how long the hosted subtitles of a whole task live
    subtitle_ffmpeg_fallback: bool = True  # ASS/SSA via ffmpeg; false = skip such files (SRT/VTT never use ffmpeg)
    cors_origins: str = "*"                # origins allowed to fetch /h/ playlists+segments: "*", "", or "https://a.com,https://b.com"
    # HLS sources: send the script's headers + User-Agent to ffmpeg? False (default) = bare ffmpeg, exactly like
    # `ffmpeg -i playlist.m3u8` by hand (some CDNs refuse requests that carry them). If a bare run fails on a
    # segment, file-mode downloads retry once WITH the headers. True = headers first (and no bare retry needed).
    hls_send_custom_headers: bool = False
    library_hls_timeout_min: int = 240   # give up on an HLS download after this long

    @property
    def library_path(self) -> Path:
        return Path(self.data_dir) / "library"


settings = Settings()


def ensure_secrets() -> None:
    """Generate ADMIN_TOKEN / SIGNING_SECRET on first run if not provided, and persist them."""
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)  # sandboxed scripts run as another user and must not read this
    except OSError:
        pass
    path = data_dir / "secrets.json"
    stored = json.loads(path.read_text()) if path.exists() else {}
    changed = False
    for field in ("admin_token", "signing_secret"):
        if getattr(settings, field):
            continue
        if field not in stored:
            stored[field] = secrets.token_urlsafe(32)
            changed = True
            if field == "admin_token":
                print(f"[candyresolver] generated ADMIN_TOKEN: {stored[field]}", flush=True)
        setattr(settings, field, stored[field])
    if changed:
        path.write_text(json.dumps(stored))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
