import re


class RangeNotSatisfiable(Exception):
    pass


def parse_range(header: str, size: int):
    """Parse a single 'bytes=a-b' range. Returns (start, end) inclusive, or None to ignore the header."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    first, last = m.groups()
    if first == "":                      # suffix range: last N bytes
        n = int(last)
        if n == 0:
            raise RangeNotSatisfiable()
        return max(size - n, 0), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    end = min(end, size - 1)
    if start >= size or start > end:
        raise RangeNotSatisfiable()
    return start, end
