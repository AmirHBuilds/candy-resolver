"""Tells when a source has failed several runs IN A ROW (a real problem, not a one-off) and when it works again. Pure logic."""


class StreakTracker:
    def __init__(self) -> None:
        self.fails: dict = {}
        self.flagged: set = set()

    def record(self, key, failed: bool, threshold: int) -> tuple[str | None, int]:
        """-> ('failing', n) the moment the n-th failure in a row reaches the threshold (once);
              ('recovered', n) when a flagged source answers again after n failures; otherwise (None, n). threshold 0 = never alert."""
        if not failed:
            n = self.fails.pop(key, 0)
            if key in self.flagged:
                self.flagged.discard(key)
                return "recovered", n
            return None, 0
        n = self.fails.get(key, 0) + 1
        self.fails[key] = n
        if threshold and threshold > 0 and n >= threshold and key not in self.flagged:
            self.flagged.add(key)
            return "failing", n
        return None, n
