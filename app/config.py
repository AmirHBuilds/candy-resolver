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
    script_memory_mb: int = 512
    script_cpu_seconds: int = 60
    script_max_output_bytes: int = 1_000_000
    script_run_as_uid: int | None = None
    script_run_as_gid: int | None = None

    # tasks
    task_ttl_hours: int = 24
    max_parallel_sources: int = 8

    # library
    library_ttl_hours: int = 24          # default auto-delete time after a download finishes
    library_max_ttl_hours: int = 168     # longest a client may ask for
    library_concurrency: int = 2         # simultaneous downloads
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
    cors_origins: str = "*"                # origins allowed to fetch /h/ playlists+segments: "*", "", or "https://a.com,https://b.com"
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
