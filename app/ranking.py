"""Which source goes first. Pure logic, no database."""


def order_key(source, starred_first: bool = False) -> tuple:
    """Lower sorts first. The admin's rank decides (1 = first); sources without a rank come last, by id.
    With starred_first (a request option) starred sources go before everything else, and rank orders within each group."""
    rank = getattr(source, "rank", None)
    return (0 if (not starred_first or source.starred) else 1,
            rank is None, rank if rank is not None else 0, source.id)


def order_rows(rows: list, starred_first: bool = False) -> list:
    """rows = [(Source, Script), ...] -> the same pairs in the order the sources should be tried."""
    return sorted(rows, key=lambda r: order_key(r[0], starred_first))


def source_stats(runs: list[tuple[str, int | None]], keep: int = 50) -> dict:
    """runs = [(status, duration_ms), ...] newest first. Response time of the latest run plus the average over
    the last `keep` runs that actually answered (errors are mostly timeouts and would hide how fast the source really is)."""
    recent = runs[:keep]
    answered = [ms for st, ms in recent if st != "error" and ms is not None]
    return {
        "last_ms": runs[0][1] if runs else None,
        "last_status": runs[0][0] if runs else None,
        "avg_ms": int(sum(answered) / len(answered)) if answered else None,
        "stats_runs": len(recent),
        "found_rate": round(100 * sum(1 for st, _ in recent if st == "ok") / len(recent)) if recent else None,
    }
