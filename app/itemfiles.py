"""The ONE place that deletes an item's folder. Video, playlist, segments and subs/ all live in it,
so everything goes together: TTL expiry, DELETE, failed downloads (and the startup sweep removes strays)."""
import re
import shutil

from .config import settings

_ID = re.compile(r"lib_[0-9a-f]{16}")


def item_dir(item_id: str):
    return settings.library_path / item_id


def remove_item_dir(item_id: str) -> None:
    if _ID.fullmatch(item_id or ""):
        shutil.rmtree(item_dir(item_id), ignore_errors=True)
