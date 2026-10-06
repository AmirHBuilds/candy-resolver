"""Downloads a stream into the library (our own disk) in the background."""
import asyncio
import os
import shutil
import time
from datetime import timedelta

import httpx

from .config import settings
from .db import SessionLocal
from .fileutil import pick_ext
from .models import LibraryItem, utcnow

GB = 1024 ** 3
MB = 1024 ** 2

downloads: dict[str, asyncio.Task] = {}
_sem: asyncio.Semaphore | None = None


class DownloadError(Exception):
    pass


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(settings.library_concurrency)
    return _sem


def start_download(item_id: str, url: str, headers: dict | None, fmt: str | None) -> None:
    t = asyncio.create_task(_download(item_id, url, headers, fmt))
    downloads[item_id] = t
    t.add_done_callback(lambda _: downloads.pop(item_id, None))


def cancel_download(item_id: str) -> bool:
    t = downloads.get(item_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


def _free_bytes() -> int:
    return shutil.disk_usage(settings.library_path).free


async def _download(item_id: str, url: str, headers: dict | None, fmt: str | None) -> None:
    async with _semaphore():
        async with SessionLocal() as db:
            item = await db.get(LibraryItem, item_id)
            if item is None or item.status != "queued":
                return  # deleted before it started
            item.status = "downloading"
            await db.commit()

            dest_dir = settings.library_path / item_id
            part = dest_dir / "video.part"
            final = dest_dir / f"video.{pick_ext(fmt, url)}"
            max_bytes = settings.library_max_size_gb * GB
            reserve = settings.library_min_free_gb * GB
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                hdrs = {"User-Agent": "candyresolver/0.1", "Accept-Encoding": "identity"}
                hdrs.update({str(k): str(v) for k, v in (headers or {}).items()})

                async with httpx.AsyncClient(follow_redirects=True,
                                             timeout=httpx.Timeout(30.0, read=60.0)) as client:
                    async with client.stream("GET", url, headers=hdrs) as r:
                        r.raise_for_status()
                        cl = r.headers.get("content-length", "")
                        total = int(cl) if cl.isdigit() else None
                        if total and total > max_bytes:
                            raise DownloadError(f"file is larger than the {settings.library_max_size_gb} GB limit")
                        if total and _free_bytes() - total < reserve:
                            raise DownloadError("not enough free disk space")
                        item.total_bytes = total
                        await db.commit()

                        written = last_disk_check = 0
                        last_commit = time.monotonic()
                        with open(part, "wb") as f:
                            async for chunk in r.aiter_bytes(MB):
                                written += len(chunk)
                                if written > max_bytes:
                                    raise DownloadError("file exceeds the size limit")
                                if written - last_disk_check >= 64 * MB:
                                    last_disk_check = written
                                    if _free_bytes() < reserve:
                                        raise DownloadError("ran out of free disk space")
                                await asyncio.to_thread(f.write, chunk)
                                if time.monotonic() - last_commit >= 2:
                                    item.size = written
                                    await db.commit()
                                    last_commit = time.monotonic()

                if written == 0:
                    raise DownloadError("empty download")
                if total and written != total:
                    raise DownloadError(f"download incomplete ({written} of {total} bytes)")

                os.replace(part, final)
                now = utcnow()
                item.status = "ready"
                item.file_path = str(final)
                item.size = written
                item.ready_at = now
                item.delete_at = now + timedelta(hours=item.ttl_hours)
                await db.commit()
            except asyncio.CancelledError:
                shutil.rmtree(dest_dir, ignore_errors=True)  # the DELETE endpoint updates the row
                raise
            except Exception as e:
                shutil.rmtree(dest_dir, ignore_errors=True)
                await db.rollback()
                item = await db.get(LibraryItem, item_id)
                if item is not None:
                    item.status = "failed"
                    item.error = (str(e) or e.__class__.__name__)[:500]
                    item.file_path = None
                    await db.commit()
