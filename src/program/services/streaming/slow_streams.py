"""
Replace a source that is too slow to play (``stream.slow_replace``).

The streaming code reports how fast each file downloads (only the time spent
waiting on the debrid server, not idle time between reads) and every read that
timed out (``ChunksTooSlowException``). A scheduled check calls a file slow when
it had ``stall_count`` timed-out reads within ``stall_window_seconds``, or spent
most of the last ``slow_seconds`` downloading at less than ``min_speed_ratio``
times its bitrate (file size / the duration Plex reports).

Only files Plex is playing count; scans and other reads are ignored. A slow
file first gets a fresh download link (TorBox may hand out another server). If
it is still slow, its torrent is blacklisted for that movie or episode, Plex
playback is stopped with a message and the item is re-sourced, keeping its
path (the VFS hold/swap). When files from two or more different torrents are
slow at once, or a file is still slow after its fresh link moved it to another
server, the problem is probably the connection or the debrid service: nothing
is replaced and the notify URLs are told.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx
from kink import di
from loguru import logger

from program.settings import settings_manager
from program.utils.notify import ORANGE, RED, Notice, notice_for_item, send

# Timing samples are dropped after this long, and files with no activity forgotten.
HISTORY_SECONDS = 600

# Share of slow_seconds a file must have spent downloading before its speed counts.
BUSY_SHARE = 0.5

# A fresh link is tried again for a file that turns slow this long after the last try.
REFRESH_RETRY_SECONDS = 30 * 60

# "Several files slow at once" is reported at most this often.
OUTAGE_NOTIFY_SECONDS = 30 * 60

EVENT_SOURCE = "SlowStreams"


def _host(url: str | None) -> str:
    try:
        return httpx.URL(url).host if url else ""
    except Exception:
        return ""


def _mbps(bits_per_second: float) -> str:
    return f"{bits_per_second / 1_000_000:.1f} Mbps"


@dataclass
class FileStats:
    path: str
    file_size: int
    url: str = ""
    samples: deque[tuple[float, int, float]] = field(default_factory=deque)
    stalls: deque[float] = field(default_factory=deque)
    last_seen: float = 0.0

    @property
    def host(self) -> str:
        return _host(self.url)


class StreamHealth:
    """Download speed and timed-out reads per file (by original filename)."""

    def __init__(self, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._files = dict[str, FileStats]()
        self._urls = dict[str, str]()
        self._clock = clock

    def _file(self, key: str, path: str, file_size: int, url: str) -> FileStats:
        stats = self._files.get(key)

        if stats is None:
            stats = self._files[key] = FileStats(path=path, file_size=file_size)

        stats.url = url
        stats.last_seen = self._clock()

        return stats

    def record_chunk(
        self,
        key: str,
        *,
        path: str,
        file_size: int,
        url: str,
        size: int,
        seconds: float,
    ) -> None:
        with self._lock:
            stats = self._file(key, path, file_size, url)
            stats.samples.append((stats.last_seen, size, seconds))

    def record_stall(self, key: str, *, path: str, file_size: int, url: str) -> None:
        with self._lock:
            stats = self._file(key, path, file_size, url)
            stats.stalls.append(stats.last_seen)

    def snapshot(self) -> dict[str, FileStats]:
        """Copies of the files with recent activity (older ones are forgotten)."""

        now = self._clock()

        with self._lock:
            for key, stats in list(self._files.items()):
                while stats.samples and now - stats.samples[0][0] > HISTORY_SECONDS:
                    stats.samples.popleft()

                while stats.stalls and now - stats.stalls[0] > HISTORY_SECONDS:
                    stats.stalls.popleft()

                if now - stats.last_seen > HISTORY_SECONDS:
                    del self._files[key]
                    self._urls.pop(key, None)

            return {
                key: FileStats(
                    path=s.path,
                    file_size=s.file_size,
                    url=s.url,
                    samples=deque(s.samples),
                    stalls=deque(s.stalls),
                    last_seen=s.last_seen,
                )
                for key, s in self._files.items()
            }

    def clear(self, key: str) -> None:
        """Start measuring a file again (after a fresh link or a replace)."""

        with self._lock:
            if stats := self._files.get(key):
                stats.samples.clear()
                stats.stalls.clear()

    def push_url(self, key: str, url: str) -> None:
        with self._lock:
            self._urls[key] = url

    def pushed_url(self, key: str) -> str | None:
        """A fresh link the streams of this file should switch to."""

        return self._urls.get(key)

    def drop_url(self, key: str) -> None:
        with self._lock:
            self._urls.pop(key, None)


stream_health = StreamHealth()


def assess(stats: FileStats, now: float, bitrate: float | None, settings) -> str | None:
    """Why this file is too slow to play, or None."""

    stalls = [t for t in stats.stalls if now - t <= settings.stall_window_seconds]

    if len(stalls) >= settings.stall_count:
        return f"{len(stalls)} reads timed out in {settings.stall_window_seconds} s"

    if not bitrate:
        return None

    recent = [(b, s) for t, b, s in stats.samples if now - t <= settings.slow_seconds]
    busy = sum(s for _, s in recent)

    if busy < settings.slow_seconds * BUSY_SHARE:
        return None

    speed = sum(b for b, _ in recent) * 8 / busy

    if speed < settings.min_speed_ratio * bitrate:
        return f"downloading at {_mbps(speed)}, plays at {_mbps(bitrate)}"

    return None


def session_bitrate(file_size: int, sessions: list[Any]) -> float | None:
    """The file's average bitrate in bits/s, from the duration Plex reports."""

    for session in sessions:
        duration = getattr(session, "duration", None)

        if not duration:
            duration = next(
                (m.duration for m in getattr(session, "media", []) if m.duration),
                None,
            )

        if duration:
            return file_size * 8 / (duration / 1000)

    return None


@dataclass
class SlowFile:
    key: str
    stats: FileStats
    sessions: list[Any]
    reason: str
    item_id: int | None
    title: str
    infohash: str | None
    # Debrid provider of the file (only TorBox files are held while it's degraded)
    provider: str | None = None


class SlowStreamMonitor:
    def __init__(self, health: StreamHealth = stream_health, clock=time.monotonic):
        self.health = health
        self._clock = clock
        # File -> (when a fresh link was tried, the slow server it replaced)
        self._refreshed = dict[str, tuple[float, str]]()
        self._replaced = dict[int, float]()
        self._outage_notified = -OUTAGE_NOTIFY_SECONDS

    @property
    def settings(self):
        return settings_manager.settings.stream.slow_replace

    def run(self) -> None:
        """Scheduled check."""

        if not self.settings.enabled:
            return

        try:
            self.check()
        except Exception as e:
            logger.warning(f"Slow streams: check failed: {e}")

    def check(self) -> None:
        files = self.health.snapshot()

        for key in list(self._refreshed):
            if key not in files:
                self._refreshed.pop(key)

        if not files:
            return

        plex = self._plex()

        if plex is None:
            return

        now = self._clock()
        slow = self._find_slow(files, plex, now)

        if not slow:
            return

        if len({f.infohash or f.key for f in slow}) > 1:
            self._outage(slow, now)
            return

        for file in slow:
            self._handle(file, now)

    # --- steps ---

    def _find_slow(
        self, files: dict[str, FileStats], plex: Any, now: float
    ) -> list[SlowFile]:
        slow = list[SlowFile]()

        for key, stats in files.items():
            sessions = plex.sessions_playing([self._library_path(stats.path)])

            if not sessions:
                continue

            reason = assess(
                stats, now, session_bitrate(stats.file_size, sessions), self.settings
            )

            if reason:
                slow.append(self._describe(key, stats, sessions, reason))

        return slow

    def _outage(self, slow: list[SlowFile], now: float) -> None:
        for file in slow:
            self.health.clear(file.key)

        message = (
            f"{len(slow)} files from different torrents are playing too slowly at "
            f"once ({'; '.join(f'{f.title}: {f.reason}' for f in slow)}). This "
            "looks like a connection or debrid service problem, so nothing was "
            "replaced."
        )
        self._connection_problem(message, now)

    def _connection_problem(self, message: str, now: float) -> None:
        logger.warning(f"Slow streams: {message}")

        if now - self._outage_notified >= OUTAGE_NOTIFY_SECONDS:
            self._outage_notified = now
            self._notify(
                Notice(
                    event="Connection Problem",
                    title="Slow streams, nothing replaced",
                    description=message,
                    color=RED,
                )
            )

    def _handle(self, file: SlowFile, now: float) -> None:
        refreshed = self._refreshed.get(file.key)

        if refreshed is None or now - refreshed[0] > REFRESH_RETRY_SECONDS:
            self._refreshed[file.key] = (now, file.stats.host)
            self.health.clear(file.key)

            fresh = self._fresh_link(file)

            if fresh:
                return

            if fresh is None:
                # The debrid service couldn't be asked: don't replace a file
                # over that; try a fresh link again at the next slow check
                self._refreshed.pop(file.key, None)
                self._connection_problem(
                    f"{file.title} is slow ({file.reason}, server "
                    f"{file.stats.host}) and no fresh link could be fetched. "
                    "This looks like a debrid service problem, so nothing was "
                    "replaced.",
                    now,
                )
                return
        elif file.stats.host and file.stats.host != refreshed[1]:
            # Slow on the old server and on the new one: probably the connection
            self.health.clear(file.key)
            self._connection_problem(
                f"{file.title} is slow on two different servers ({refreshed[1]}, "
                f"then {file.stats.host}: {file.reason}). This looks like a "
                "connection or debrid service problem, so nothing was replaced.",
                now,
            )
            return

        if file.item_id is None:
            return

        from program.services.downloaders.torbox_health import health

        # TorBox (or this server's CDN region) degraded: the file is probably
        # fine; judge it afresh once TorBox recovers
        if health.hold(
            f"{file.title} (slow: {file.reason}, server {file.stats.host}; not replaced)",
            host=file.stats.host,
            provider=file.provider,
        ):
            self.health.clear(file.key)
            self._refreshed.pop(file.key, None)
            return

        last = self._replaced.get(file.item_id)

        if last is not None and now - last < self.settings.cooldown_hours * 3600:
            logger.debug(
                f"Slow streams: {file.title} is slow ({file.reason}) but was "
                "replaced recently; leaving it"
            )
            self.health.clear(file.key)
            return

        self._replaced[file.item_id] = now
        self.health.clear(file.key)
        self._replace(file)

    def _fresh_link(self, file: SlowFile) -> bool | None:
        """Point the file's streams at a fresh download link.

        Returns True if nothing more is needed (the link changed, or the file
        was already re-sourced), False if there is no other link, and None if
        the debrid service couldn't give one (API error or outage).

        A fresh link that is gone says nothing about the one being played, which
        is still answering: that only means there is no other link. If the
        current link fails too, the read that hits it starts the re-source.
        """

        from program.services.filesystem.vfs import VFSDatabase

        try:
            entry = di[VFSDatabase].get_entry_by_original_filename(
                original_filename=file.key, force_resolve=True, reset_if_gone=False
            )
        except Exception as e:
            logger.debug(f"Slow streams: no fresh link for {file.title}: {e}")
            return None

        if entry and entry.link_gone:
            logger.info(
                f"Slow streams: {file.title} is slow ({file.reason}, server "
                f"{file.stats.host}); its fresh link is gone, so no other link to try"
            )
            return False

        if entry is None:
            # These stats are for a file a re-source has already swapped out
            logger.debug(f"Slow streams: {file.title} is no longer in use")
            return True

        url = entry.url

        if not url:
            logger.info(
                f"Slow streams: {file.title} is slow ({file.reason}, server "
                f"{file.stats.host}); the debrid service gave no fresh link"
            )
            return None

        if url == file.stats.url:
            logger.info(
                f"Slow streams: {file.title} is slow ({file.reason}, server "
                f"{file.stats.host}); no other link to try"
            )
            return False

        self.health.push_url(file.key, url)
        logger.info(
            f"Slow streams: {file.title} is slow ({file.reason}, server "
            f"{file.stats.host}); trying a fresh link (server {_host(url)})"
        )
        return True

    def _replace(self, file: SlowFile) -> None:
        from sqlalchemy.orm import Session

        from program.db.db import db_session
        from program.media.item import MediaItem
        from program.media.stream import BlacklistReason
        from program.services.downloaders.torbox_import import account_import
        from program.types import Event
        from routers.secure.items import apply_item_mutation

        summary = f"{file.title} was too slow to play ({file.reason}, server {file.stats.host})"

        for session in file.sessions:
            try:
                session.stop(reason=self.settings.stop_playback_message)
            except Exception as e:
                message = f"{summary}, but stopping the Plex playback failed ({e}); not replaced"
                logger.warning(f"Slow streams: {message}")
                self._notify(
                    notice_for_item(
                        Notice(
                            event="Slow Stream Not Replaced",
                            title=file.title,
                            description="Too slow to play, but stopping the Plex playback failed.",
                            color=RED,
                            fields=[
                                ("Reason", file.reason),
                                ("Server", file.stats.host or ""),
                                ("Error", str(e)),
                            ],
                        ),
                        file.item_id,
                    )
                )
                return

        program = self._program()

        if vfs := program.services.filesystem.riven_vfs:
            vfs.playback_stopped(file.key)

        with db_session() as session:
            item = session.get(MediaItem, file.item_id)

            if item is None:
                return

            def mutation(i: MediaItem, s: Session) -> None:  # noqa: ARG001
                i.blacklist_active_stream(BlacklistReason.SLOW_SOURCE)
                i.reset(keep_vfs=True)

            apply_item_mutation(
                program=program, item=item, mutation_fn=mutation, session=session
            )
            session.commit()

        self.health.drop_url(file.key)
        self._refreshed.pop(file.key, None)

        message = f"{summary}. Stopped the Plex playback and finding another copy."
        logger.info(f"Slow streams: {message}")
        self._notify(
            notice_for_item(
                Notice(
                    event="Slow Stream Replaced",
                    title=file.title,
                    description="Too slow to play. Stopped the Plex playback and finding another copy.",
                    color=ORANGE,
                    fields=[("Reason", file.reason), ("Server", file.stats.host or "")],
                ),
                file.item_id,
            )
        )

        # Another torrent on the account may cover the item already
        if not account_import.offer(file.item_id):
            program.em.add_event(Event(EVENT_SOURCE, file.item_id))

    # --- helpers ---

    def _describe(
        self, key: str, stats: FileStats, sessions: list[Any], reason: str
    ) -> SlowFile:
        from sqlalchemy import select

        from program.db.db import db_session
        from program.media.media_entry import MediaEntry

        item_id, title, infohash = None, os.path.basename(stats.path), None
        provider = None

        with db_session() as session:
            entry = session.scalars(
                select(MediaEntry).where(MediaEntry.original_filename == key)
            ).first()

            if entry:
                provider = entry.provider

            if entry and (item := entry.media_item):
                item_id, title = item.id, item.log_string
                infohash = item.active_stream.infohash if item.active_stream else None

        return SlowFile(key, stats, sessions, reason, item_id, title, infohash, provider)

    @staticmethod
    def _program():
        from program.program import Program

        return di[Program]

    def _plex(self):
        from program.services.updaters.plex import PlexUpdater

        services = self._program().services

        if not services:
            return None

        plex = services.updater.services.get(PlexUpdater)

        return plex if plex and plex.initialized else None

    def _library_path(self, path: str) -> str:
        return os.path.join(
            self._program().services.updater.library_path, path.lstrip("/")
        )

    def _notify(self, notice: Notice) -> None:
        if self.settings.notify_urls:
            send(self.settings.notify_urls, notice)


slow_stream_monitor_instance = SlowStreamMonitor()


def slow_stream_monitor() -> None:
    slow_stream_monitor_instance.run()
