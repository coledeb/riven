"""
Uncached TorBox downloads, as a last resort.

When no cached torrent works for an item, the downloader can ask TorBox to
download an uncached one from peers (``downloaders.torbox.uncached``). The
pick: quality first (resolution, then seeder tier) or seeders first (tier,
then quality), per ``prefer``. While TorBox downloads, the item waits and is checked every
``check_minutes``; once finished, the torrent is cached and handled like any
other. A download with no progress for ``stall_minutes`` is deleted and its
torrent blacklisted for the item, so the next candidate gets a turn.

Only one download runs per item, and none while an item above or below it
(its season or show, or one of its episodes) has one running. New downloads
start only while the account has a free slot (``active_download_slots``).
Running downloads are kept in data/torbox_uncached.json across restarts.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from program.settings import settings_manager
from program.utils import data_dir_path
from program.utils.notify import PURPLE, RED, Notice, item_title, notice_for_item, send
from program.utils.request import CircuitBreakerOpen

if TYPE_CHECKING:
    from program.media.item import MediaItem
    from program.media.stream import Stream
    from program.services.downloaders.torbox import TorBoxDownloader

STATE_FILE = "torbox_uncached.json"

# How long a "not cached" answer is trusted before the stream is checked again
NOT_CACHED_TTL_SECONDS = 3600

# Wait before looking for a free download slot again
NO_SLOT_WAIT_MINUTES = 15

# Progress (percent) that counts as moving
MIN_PROGRESS_STEP = 0.1


@dataclass
class UncachedDownload:
    infohash: str
    torrent_id: str
    item_id: str
    # Ids of the item's season and show, so related items wait for it
    ancestors: list[str]
    item_name: str
    title: str
    seeders: int | None
    started: str
    progress: float = 0.0
    progress_at: float = 0.0  # wall-clock seconds; survives restarts


@dataclass
class Check:
    state: Literal["wait", "done", "failed"]
    infohash: str | None = None
    run_at: datetime | None = None


def _ancestor_ids(item: "MediaItem") -> list[str]:
    ids = list[str]()
    parent = getattr(item, "parent", None)

    while parent is not None and getattr(parent, "id", None) is not None:
        ids.append(str(parent.id))
        parent = getattr(parent, "parent", None)

    return ids


class UncachedDownloads:
    def __init__(self, torbox: "TorBoxDownloader") -> None:
        self.torbox = torbox
        self._lock = threading.Lock()
        self._file = data_dir_path / STATE_FILE
        self._running = dict[str, UncachedDownload]()  # by item id
        # item id -> {infohash: monotonic time TorBox said "not cached"}
        self._not_cached = dict[str, dict[str, float]]()
        self._load()

    @property
    def settings(self):
        return self.torbox.settings.uncached

    @property
    def enabled(self) -> bool:
        return bool(self.torbox.initialized and self.settings.enabled)

    # --- persistence ---

    def _load(self) -> None:
        try:
            data = json.loads(self._file.read_text())
            self._running = {
                str(k): UncachedDownload(**v) for k, v in data.items()
            }
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"TorBox: ignoring unreadable {self._file}: {e}")

    def _save(self) -> None:
        """Write the running downloads; call with self._lock held."""

        try:
            self._file.write_text(
                json.dumps({k: asdict(v) for k, v in self._running.items()})
            )
        except Exception as e:
            logger.warning(f"TorBox: couldn't save {self._file}: {e}")

    def torrent_ids(self) -> set[str]:
        """Torrents still downloading for an item (never 'unused')."""

        with self._lock:
            return {d.torrent_id for d in self._running.values()}

    # --- streams TorBox doesn't have cached ---

    def remember_not_cached(self, item_id: Any, infohash: str) -> None:
        with self._lock:
            self._not_cached.setdefault(str(item_id), {})[infohash.lower()] = (
                time.monotonic()
            )

    def not_cached(self, item_id: Any) -> set[str]:
        """Infohashes TorBox recently reported as not cached for this item."""

        now = time.monotonic()

        with self._lock:
            seen = self._not_cached.get(str(item_id), {})

            for infohash, at in list(seen.items()):
                if now - at > NOT_CACHED_TTL_SECONDS:
                    del seen[infohash]

            return set(seen)

    def forget(self, item_id: Any, infohash: str | None = None) -> None:
        with self._lock:
            if infohash is None:
                self._not_cached.pop(str(item_id), None)
            else:
                self._not_cached.get(str(item_id), {}).pop(infohash.lower(), None)

    def eligible(self, stream: "Stream") -> bool:
        return (stream.seeders or 0) >= min(self.settings.seeder_tiers)

    def candidates(
        self, item: "MediaItem", streams: list["Stream"]
    ) -> list["Stream"]:
        """
        Not-cached streams worth downloading, best first.

        `streams` is in quality order. prefer="quality": resolution first,
        then seeder tier, then the rest of the order. prefer="seeders":
        seeder tier first, then the quality order.
        """

        from program.services.downloaders.shared import get_resolution

        tiers = self.settings.seeder_tiers
        seen = self.not_cached(item.id)

        def tier(stream: "Stream") -> int:
            return next(i for i, t in enumerate(tiers) if (stream.seeders or 0) >= t)

        pool = [s for s in streams if s.infohash.lower() in seen and self.eligible(s)]

        if self.settings.prefer == "seeders":
            return sorted(pool, key=lambda s: (tier(s), streams.index(s)))

        return sorted(
            pool,
            key=lambda s: (-get_resolution(s).value, tier(s), streams.index(s)),
        )

    # --- running downloads ---

    def check(self, item: "MediaItem") -> Check | None:
        """
        Where the item stands with uncached downloads.

        None: nothing running for it or a related item. "wait": come back at
        run_at. "done": the torrent finished (now cached). "failed": it was
        given up and deleted; blacklist `infohash` for the item.
        """

        item_id = str(item.id)
        ancestors = set(_ancestor_ids(item))

        with self._lock:
            own = self._running.get(item_id)
            blocked = own is None and any(
                d.item_id in ancestors or item_id in d.ancestors
                for d in self._running.values()
            )

        wait = Check("wait", run_at=self._next_check())

        if blocked:
            logger.debug(
                f"{item.log_string}: a related item has an uncached TorBox download running"
            )
            return wait

        if own is None:
            return None

        from program.services.downloaders.torbox import TorBoxNotFound

        try:
            info = self.torbox.get_torrent_info(own.torrent_id)
        except TorBoxNotFound:
            return self._give_up(own, "it was removed from TorBox")
        except CircuitBreakerOpen:
            return wait
        except Exception as e:
            logger.debug(f"TorBox: couldn't check download {own.torrent_id}: {e}")
            return wait

        if info.status == "downloaded":
            with self._lock:
                self._running.pop(item_id, None)
                self._save()

            logger.info(
                f"Uncached TorBox download finished for {own.item_name}: {own.title}"
            )
            return Check("done", infohash=own.infohash)

        status = (info.status or "").lower()

        if "error" in status or "failed" in status:
            return self._give_up(own, f"TorBox reported '{info.status}'")

        now = time.time()
        progress = info.progress or 0.0

        if progress >= own.progress + MIN_PROGRESS_STEP:
            with self._lock:
                own.progress = progress
                own.progress_at = now
                self._save()
        elif now - own.progress_at > self.settings.stall_minutes * 60:
            return self._give_up(
                own,
                f"no progress for {self.settings.stall_minutes} min "
                f"(at {progress:.0f}%, {info.status})",
            )

        logger.debug(
            f"{own.item_name}: uncached TorBox download at {progress:.0f}% ({info.status})"
        )
        return wait

    def _give_up(self, download: UncachedDownload, reason: str) -> Check:
        with self._lock:
            self._running.pop(download.item_id, None)
            self._save()

        try:
            self.torbox.delete_torrent(download.torrent_id)
        except Exception as e:
            logger.debug(f"TorBox: couldn't delete download {download.torrent_id}: {e}")

        message = (
            f"Gave up the uncached download for {download.item_name}: {reason}.\n"
            f"{download.title}\nThe next candidate (if any) is tried."
        )
        logger.warning(f"TorBox: {message}")
        self._notify(
            notice_for_item(
                Notice(
                    event="Uncached Download Failed",
                    title=download.item_name,
                    description="TorBox gave up on this download; the next candidate (if any) is tried.",
                    color=RED,
                    fields=[("Reason", reason), ("Torrent", download.title)],
                ),
                download.item_id,
            )
        )

        return Check("failed", infohash=download.infohash)

    def free_slots(self) -> tuple[int, int]:
        """(downloads running on the account, plan slots)."""

        busy = sum(
            1
            for t in self.torbox.list_torrents()
            if not t.get("download_finished") and t.get("active", True)
        )

        return busy, self.torbox.settings.active_download_slots

    def start(self, item: "MediaItem", stream: "Stream") -> Check | None:
        """
        Start downloading `stream` for the item.

        Returns a "wait" Check (started, or no free slot), or None when TorBox
        refused the add (blacklist the stream and try the next one).
        """

        busy, slots = self.free_slots()

        if busy >= slots:
            logger.debug(
                f"{item.log_string}: all {slots} TorBox download slots busy; waiting"
            )
            return Check(
                "wait",
                run_at=datetime.now() + timedelta(minutes=NO_SLOT_WAIT_MINUTES),
            )

        try:
            torrent_id = self.torbox.add_torrent(stream.infohash, only_if_cached=False)
        except CircuitBreakerOpen:
            raise
        except Exception as e:
            logger.debug(f"TorBox: uncached add of {stream.infohash} failed: {e}")
            return None

        download = UncachedDownload(
            infohash=stream.infohash.lower(),
            torrent_id=str(torrent_id),
            item_id=str(item.id),
            ancestors=_ancestor_ids(item),
            item_name=item.log_string,
            title=stream.raw_title,
            seeders=stream.seeders,
            started=datetime.now().isoformat(timespec="seconds"),
            progress_at=time.time(),
        )

        with self._lock:
            self._running[download.item_id] = download
            self._save()

        message = (
            f"{item.log_string} isn't cached on TorBox, so TorBox is downloading it.\n"
            f"{stream.raw_title}\n"
            f"Seeders reported: {stream.seeders} · download slot {busy + 1}/{slots}"
        )
        logger.info(f"Started uncached TorBox download: {message}")
        self._notify(
            Notice(
                event="Uncached Download Started",
                title=item_title(item),
                description="Not cached on TorBox, so TorBox is downloading it.",
                color=PURPLE,
                fields=[
                    ("Seeders", str(stream.seeders)),
                    ("Download Slot", f"{busy + 1}/{slots}"),
                    ("Torrent", stream.raw_title),
                ],
            ).with_item(item, settings_manager.settings.notifications.link_url)
        )

        return Check("wait", run_at=self._next_check())

    def _next_check(self) -> datetime:
        return datetime.now() + timedelta(minutes=self.settings.check_minutes)

    def _notify(self, notice: Notice) -> None:
        if self.settings.notify_urls:
            send(self.settings.notify_urls, notice)
