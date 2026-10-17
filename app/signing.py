import hashlib
import hmac
import time

from .config import settings


def _mac(item_id: str, exp: int) -> str:
    return hmac.new(settings.signing_secret.encode(), f"{item_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def sign(item_id: str, ttl_s: int) -> tuple[int, str]:
    exp = int(time.time()) + max(ttl_s, 1)
    return exp, _mac(item_id, exp)


def verify(item_id: str, exp, sig) -> bool:
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    try:
        return exp >= time.time() and hmac.compare_digest(_mac(item_id, exp), sig or "")
    except TypeError:           # non-ASCII text in the signature
        return False
