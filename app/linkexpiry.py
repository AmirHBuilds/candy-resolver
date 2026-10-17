"""When does a source's stream link stop working, and did a download fail because it already had? Pure logic, no database."""
import base64
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

_EPOCH_KEYS = {"exp", "expires", "expire", "expiry", "expires_at", "e"}
_DEAD = re.compile(
    r"(?:HTTP(?:\s+error)?|Server returned|Client error|status(?:\s+code)?|->\s*HTTP)\W{0,3}(401|403|404|410)\b"
    r"|\b(401 Unauthorized|403 Forbidden|404 Not Found|410 Gone)\b", re.I)


def _epoch(value) -> datetime | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n > 1e12:                       # milliseconds
        n /= 1000
    if not 1_500_000_000 < n < 4_000_000_000:
        return None
    return datetime.fromtimestamp(n, tz=timezone.utc)


def _jwt_exp(value: str) -> datetime | None:
    parts = value.strip().removeprefix("Bearer ").split(".")
    if len(parts) != 3 or len(parts[1]) < 8:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return None
    return _epoch(payload.get("exp")) if isinstance(payload, dict) else None


def link_expiry(url: str, headers: dict | None = None) -> datetime | None:
    """The earliest expiry found in the link: a JWT `exp` (in any query value or header) or a plain epoch
    parameter such as `expires=` / `exp=`. None = the link carries no expiry we can read."""
    found: list[datetime] = []
    try:
        values = list(parse_qsl(urlsplit(url).query, keep_blank_values=False))
    except ValueError:
        values = []
    for key, val in values:
        d = _jwt_exp(val) if "." in val else (_epoch(val) if key.lower() in _EPOCH_KEYS else None)
        if d:
            found.append(d)
    for val in (headers or {}).values():
        if isinstance(val, str) and "." in val:
            d = _jwt_exp(val)
            if d:
                found.append(d)
    return min(found) if found else None


def dead_link_reason(error_text: str | None) -> str | None:
    """\"HTTP 401\" when a download error says the SOURCE refused the link (401/403/404/410), else None."""
    m = _DEAD.search(error_text or "")
    if not m:
        return None
    return "HTTP " + (m.group(1) or m.group(2).split()[0])
