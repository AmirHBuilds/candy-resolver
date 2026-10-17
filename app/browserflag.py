"""Which sources are "browser sources" (heavy: Chrome + a virtual display, ~400 MB each)?

The flag Source.uses_browser is detected from the script's code when a script is uploaded/activated, unless the
admin set it by hand (Source.browser_manual): a manual choice always wins and is never overwritten.
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .db import SessionLocal
from .models import Source

# 1) importing a browser-automation library, 2) launching a browser by name (anywhere in the code)
_MODULES = ("playwright", "pyvirtualdisplay", "selenium", "seleniumbase", "undetected_chromedriver", "pyppeteer",
            "nodriver", "zendriver", "camoufox", "patchright", "splinter", "DrissionPage")
_IMPORT = re.compile(r"^\s*(?:from|import)\s+(?:" + "|".join(_MODULES) + r")\b", re.M)
_LAUNCH = re.compile(r"\bXvfb\b|chromium\.launch|launch_persistent_context|chromedriver|geckodriver", re.I)


def detect_browser(code: str | None) -> bool:
    """True if the script looks like it drives a real browser (comments are ignored)."""
    if not code:
        return False
    stripped = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
    return bool(_IMPORT.search(stripped) or _LAUNCH.search(stripped))


def active_code(source: Source) -> str | None:
    return next((sc.code for sc in source.scripts if sc.active), None)


def apply_detection(source: Source, code: str | None) -> None:
    """Set the flag from `code` unless the admin chose it by hand."""
    if not source.browser_manual:
        source.uses_browser = detect_browser(code)


async def backfill_browser_flags() -> None:
    """On startup: sources that were never set by hand follow their active script (also fixes pre-existing rows)."""
    async with SessionLocal() as db:
        rows = (await db.execute(select(Source).where(Source.browser_manual.is_(False))
                                 .options(selectinload(Source.scripts)))).scalars().all()
        for s in rows:
            s.uses_browser = detect_browser(active_code(s))
        await db.commit()
