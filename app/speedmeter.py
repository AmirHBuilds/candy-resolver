"""Smoothed download speed + ETA, and the in-memory registry the API reads for items that are downloading."""
import time
from collections import deque


class SpeedMeter:
    """Average speed over roughly the last `window` seconds (not an instant value, so it doesn't jump around)."""

    def __init__(self, window: float = 8.0):
        self.window = window
        self._s: deque[tuple[float, int]] = deque()

    def add(self, size: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._s.append((now, size))
        while len(self._s) > 2 and self._s[1][0] <= now - self.window:
            self._s.popleft()

    def speed(self) -> int | None:
        """Bytes per second, or None when there is not enough data yet."""
        if len(self._s) < 2:
            return None
        (t0, s0), (t1, s1) = self._s[0], self._s[-1]
        if t1 - t0 < 0.5:
            return None
        return max(0, int((s1 - s0) / (t1 - t0)))

    def eta(self, remaining: int | None) -> int | None:
        sp = self.speed()
        if not sp or remaining is None:
            return None
        return max(0, int(remaining / sp + 0.5))


# item id -> (speed, eta). Only meaningful while an item is downloading; cleared when the download ends.
_live: dict[str, tuple[int | None, int | None]] = {}


def publish(item_id: str, speed: int | None, eta: int | None) -> None:
    _live[item_id] = (speed, eta)


def read(item_id: str) -> tuple[int | None, int | None]:
    return _live.get(item_id, (None, None))


def clear(item_id: str) -> None:
    _live.pop(item_id, None)
