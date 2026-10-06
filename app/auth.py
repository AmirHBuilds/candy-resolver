import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import ApiKey, utcnow


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    return "cr_" + secrets.token_urlsafe(32)


# Simple in-memory sliding window (per process). Move to Redis if you run several workers.
_hits: dict[int, deque] = defaultdict(deque)


def _check_rate(key: ApiKey) -> None:
    now = time.monotonic()
    q = _hits[key.id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= key.rate_limit_per_min:
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
    q.append(now)


async def require_api_key(x_api_key: str | None = Header(default=None),
                          db: AsyncSession = Depends(get_db)) -> ApiKey:
    if not x_api_key:
        raise HTTPException(401, "missing X-API-Key header")
    res = await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(x_api_key)))
    key = res.scalar_one_or_none()
    if key is None or not key.enabled:
        raise HTTPException(401, "invalid api key")
    _check_rate(key)
    key.last_used_at = utcnow()
    await db.commit()
    return key


async def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(401, "invalid admin token")
