"""Live log of an admin 'Test run': what the script prints plus what the runner does, readable while it is still running.
Kept in memory only (a test is a development tool). No database, no web framework."""
import codecs
import secrets
import time

MAX_LINES = 5000
MAX_LINE_CHARS = 2000
KEEP_RUNS = 20
KEEP_SECONDS = 3600


class TestRun:
    __test__ = False                       # not a pytest class

    def __init__(self, source_name: str, version: int):
        self.id = secrets.token_hex(6)
        self.source_name, self.version = source_name, version
        self.t0 = time.monotonic()
        self.finished_at: float | None = None
        self.status = "running"            # running | done | cancelled
        self.result: dict | None = None
        self.task = None                   # the asyncio task, so it can be cancelled
        self.lines: list[dict] = []
        self._partial = ""
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._truncated = False

    # ---- writing ----
    def _add(self, src: str, text: str) -> None:
        if len(self.lines) >= MAX_LINES:
            if not self._truncated:
                self._truncated = True
                self.lines.append({"n": len(self.lines), "t": self._now(), "src": "runner",
                                   "text": f"... log stopped after {MAX_LINES} lines (the script keeps running)"})
            return
        self.lines.append({"n": len(self.lines), "t": self._now(), "src": src, "text": text[:MAX_LINE_CHARS]})

    def _now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def log(self, src: str, text: str) -> None:
        """One message from the runner itself (src='runner')."""
        for line in str(text).splitlines() or [""]:
            self._add(src, line)

    def feed(self, data: bytes) -> None:
        """Raw script output as it arrives (any chunking). Complete lines are published at once."""
        self._partial += self._dec.decode(data).replace("\r\n", "\n").replace("\r", "\n")
        *done, self._partial = self._partial.split("\n")
        for line in done:
            self._add("script", line)
        if len(self._partial) > MAX_LINE_CHARS:          # a very long line without newline: do not wait for it
            self._add("script", self._partial)
            self._partial = ""

    def finish(self, status: str, result: dict | None) -> None:
        if self._partial:
            self._add("script", self._partial)
            self._partial = ""
        self.status, self.result, self.finished_at = status, result, time.monotonic()

    # ---- reading ----
    def since(self, after: int = 0) -> dict:
        after = max(0, after)
        return {"test_id": self.id, "status": self.status, "source": self.source_name, "version": self.version,
                "logs": self.lines[after:], "next": len(self.lines),
                "result": self.result if self.status != "running" else None}


_runs: dict[str, TestRun] = {}


def create(source_name: str, version: int) -> TestRun:
    now = time.monotonic()
    for rid, r in list(_runs.items()):                   # forget old finished tests
        if r.finished_at is not None and now - r.finished_at > KEEP_SECONDS:
            del _runs[rid]
    finished = sorted((r for r in _runs.values() if r.finished_at is not None), key=lambda r: r.finished_at)
    while len(_runs) >= KEEP_RUNS and finished:
        del _runs[finished.pop(0).id]
    run = TestRun(source_name, version)
    _runs[run.id] = run
    return run


def get(test_id: str) -> TestRun | None:
    return _runs.get(test_id)
