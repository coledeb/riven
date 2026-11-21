"""Paces the per-episode fallback scrape of seasons whose pack search failed.

When that fallback first runs (or after a library import), every stuck season
sends all of its aired episodes to the scraper at once. Hundreds of scrapes,
downloads and new files for Plex to analyse arrive at once (and Torrentio
answers with 429s). The pacer lets a few episodes through at a time, caps the
total per window, and asks for the season to be tried again later for the
rest.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass

WINDOW_SECONDS = 10 * 60
MAX_PER_WINDOW = 60  # episodes started per window, across all seasons
BATCH_PER_SEASON = 10  # episodes one season may start per pass
RETRY_SECONDS = 60  # wait before a season sends its next batch


@dataclass(frozen=True)
class Grant:
    allowed: list  # the episodes to scrape now
    retry_in: float | None  # seconds until the season should try again; None = done


class EpisodeFallbackPacer:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._started = deque[float]()  # start times inside the window
        self._recent = dict[Hashable, float]()  # episode key -> start time

    def reset(self) -> None:
        with self._lock:
            self._started.clear()
            self._recent.clear()

    def take(
        self,
        episodes: list,
        key: Callable[[object], Hashable] = id,
    ) -> Grant:
        """Pick which of a season's due episodes may be scraped now."""

        with self._lock:
            now = self._clock()

            while self._started and now - self._started[0] >= WINDOW_SECONDS:
                self._started.popleft()

            for k, t in list(self._recent.items()):
                if now - t >= WINDOW_SECONDS:
                    del self._recent[k]

            # Episodes started recently may still be waiting in the scraper
            wanted = [e for e in episodes if key(e) not in self._recent]

            if not wanted:
                return Grant([], None)

            room = MAX_PER_WINDOW - len(self._started)
            allowed = wanted[: max(0, min(BATCH_PER_SEASON, room))]

            for e in allowed:
                self._started.append(now)
                self._recent[key(e)] = now

            if len(allowed) == len(wanted):
                return Grant(allowed, None)

            retry_in = float(RETRY_SECONDS)

            if len(self._started) >= MAX_PER_WINDOW:
                retry_in = max(
                    retry_in, WINDOW_SECONDS - (now - self._started[0]) + 1
                )

            return Grant(allowed, retry_in)


pacer = EpisodeFallbackPacer()
