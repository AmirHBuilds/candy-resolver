"""Admin API for the Telegram alerts: connection, one topic per kind of message, test message, finding chat/topic ids."""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_admin
from .. import reportjob
from ..notify import CATEGORIES, TOKEN_RE, notifier

router = APIRouter(prefix="/admin/telegram", tags=["admin-telegram"], dependencies=[Depends(require_admin)])
_CHAT = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,}")


class CategoryIn(BaseModel):
    enabled: bool | None = None
    topic_id: int | None = Field(default=None, ge=1, description="Topic (message_thread_id) inside the group. Empty = the group's General chat")
    chat_id: str | None = Field(default=None, description="Send this kind of message to ANOTHER chat instead of the main one")


class ReportIn(BaseModel):
    enabled: bool | None = None
    time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="HH:MM in the chosen time zone")
    utc_offset: float | None = Field(default=None, ge=-12, le=14, description="Your time zone: hours from UTC (e.g. 3.5)")


class TelegramIn(BaseModel):
    enabled: bool | None = None
    bot_token: str | None = Field(default=None, description="null = keep the saved token, empty string = remove it")
    chat_id: str | None = None
    server_name: str | None = Field(default=None, max_length=60)
    api_base: str | None = Field(default=None, description="Only if api.telegram.org is blocked from your server: your own relay")
    dedupe_s: int | None = Field(default=None, ge=0, le=86400, description="The same error is sent once per this many seconds")
    rate_per_min: int | None = Field(default=None, ge=1, le=60)
    low_disk_gb: float | None = Field(default=None, ge=0, le=100000, description="Warn when free disk space is below this many GB (default 2). 0 = never")
    low_disk_repeat_h: int | None = Field(default=None, ge=1, le=168, description="While the disk stays low, warn again every this many hours")
    fail_streak: int | None = Field(default=None, ge=0, le=100, description="Alert when a source fails this many runs in a row (default 5). 0 = never")
    report: ReportIn | None = None
    categories: dict[str, CategoryIn] | None = None


class TelegramTestIn(BaseModel):
    category: str | None = None


@router.get("")
async def get_telegram():
    return notifier.public()


@router.put("")
async def put_telegram(body: TelegramIn):
    patch = body.model_dump(exclude_unset=True)
    if "bot_token" in patch and patch["bot_token"] is not None:
        patch["bot_token"] = patch["bot_token"].strip()
        if patch["bot_token"] and not TOKEN_RE.fullmatch(patch["bot_token"]):
            raise HTTPException(422, "that does not look like a bot token (it is like 123456789:AAH... from @BotFather)")
    for k in ("chat_id",):
        if patch.get(k) is not None:
            patch[k] = patch[k].strip()
            if patch[k] and not _CHAT.fullmatch(patch[k]):
                raise HTTPException(422, "chat id must be a number like -1001234567890 (or @channelname)")
    if patch.get("api_base"):
        patch["api_base"] = patch["api_base"].strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s]+", patch["api_base"]):
            raise HTTPException(422, "api_base must start with http:// or https://")
    elif "api_base" in patch:
        patch["api_base"] = "https://api.telegram.org"
    cats = {}
    for key, cv in (patch.get("categories") or {}).items():
        if key not in CATEGORIES:
            raise HTTPException(422, f"unknown category '{key}'")
        c = {x: cv[x] for x in ("enabled", "topic_id") if x in cv}
        if cv.get("chat_id") is not None:
            cid = cv["chat_id"].strip()
            if cid and not _CHAT.fullmatch(cid):
                raise HTTPException(422, f"chat id of '{key}' must be a number like -1001234567890")
            c["chat_id"] = cid
        cats[key] = c
    if "categories" in patch:
        patch["categories"] = cats
    notifier.update(patch)
    return notifier.public()


@router.post("/report")
async def send_report_now():
    """Send the daily report now (to the Daily report topic) and say whether Telegram accepted it."""
    return await reportjob.send("manual")


@router.post("/test")
async def test_telegram(body: TelegramTestIn):
    """Send a test message to one kind's topic (or the System topic). Reports Telegram's answer, with a hint on what to fix."""
    if body.category is not None and body.category not in CATEGORIES:
        raise HTTPException(422, "unknown category")
    return await notifier.send_test(body.category)


@router.get("/updates")
async def telegram_updates():
    """Chats and topics the bot has recently seen: send any message in each topic, then press this to read their ids."""
    return await notifier.updates()
