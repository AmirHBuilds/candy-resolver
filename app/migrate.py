"""Tiny automatic migration: adds columns that are missing from tables created by an older version.
(create_all() creates new tables but never alters existing ones.) Replace with Alembic if the schema grows."""
from sqlalchemy import inspect, text

# (table, column, DDL type) - safe to run on every start
COLUMNS = [
    ("sources", "public_name", "VARCHAR(100) NOT NULL DEFAULT ''"),
    ("source_runs", "public_name", "VARCHAR(100)"),
    ("library_items", "public_name", "VARCHAR(100)"),
    ("library_items", "mode", "VARCHAR(10) NOT NULL DEFAULT 'file'"),
    ("library_items", "playable", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("library_items", "tmdb_id", "INTEGER"),
    ("library_items", "media_type", "VARCHAR(10)"),
    ("library_items", "season", "INTEGER"),
    ("library_items", "episode", "INTEGER"),
    ("library_items", "title", "VARCHAR(300)"),
    ("library_items", "subtitles", "JSON"),
    ("library_items", "subtitles_status", "VARCHAR(12) NOT NULL DEFAULT 'none'"),
    ("tasks", "version", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "sources_total", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "starred_total", "INTEGER NOT NULL DEFAULT 0"),
    ("sources", "starred", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("source_runs", "starred", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("sources", "uses_browser", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("sources", "browser_manual", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("source_runs", "peak_mb", "INTEGER"),
    ("library_items", "opts", "VARCHAR(160)"),
    ("tasks", "starred_first", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("library_items", "duration_s", "FLOAT"),
    ("sources", "rank", "INTEGER"),
    ("tasks", "sources_wanted", "INTEGER"),
    ("streams", "expires_at", "{DATETIME}"),
    ("streams", "dead", "BOOLEAN NOT NULL DEFAULT FALSE"),
]


def _add_missing(conn) -> None:
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    for table, column, ddl in COLUMNS:
        if table not in tables:
            continue
        if column not in {c["name"] for c in insp.get_columns(table)}:
            ddl = ddl.replace("{DATETIME}", "TIMESTAMP WITH TIME ZONE" if conn.dialect.name == "postgresql" else "DATETIME")
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            print(f"[candyresolver] migrated: added {table}.{column}", flush=True)


async def migrate(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_add_missing)
