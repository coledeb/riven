"""Seeder counts reported by scrapers, keyed by infohash.

Scrapers return {infohash: title}; the ones that know seeder counts
(Torrentio) record them here, and parse_results puts them on the Stream.
"""

import threading
import time

MAX_AGE_SECONDS = 6 * 3600

_lock = threading.Lock()
_counts = dict[str, tuple[int, float]]()


def record(infohash: str, seeders: int) -> None:
    now = time.monotonic()

    with _lock:
        _counts[infohash.lower()] = (seeders, now)

        if len(_counts) > 50_000:
            for key, (_, at) in list(_counts.items()):
                if now - at > MAX_AGE_SECONDS:
                    del _counts[key]


def get(infohash: str) -> int | None:
    with _lock:
        entry = _counts.get(infohash.lower())

    if not entry or time.monotonic() - entry[1] > MAX_AGE_SECONDS:
        return None

    return entry[0]


def parse_count(text: str) -> int | None:
    """First whole number in `text` ("12 💾 1.4 GB" → 12)."""

    for word in text.split():
        if word.isdigit():
            return int(word)

    return None
