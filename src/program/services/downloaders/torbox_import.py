"""
Link torrents added to the TorBox account outside Riven into the library.

Torrents added with another tool (for example Debrid Media Manager) are
matched to a movie or show by name, first against Riven's library and then
through a TMDB search. Their files only fill movies and episodes that have
no file yet; nothing Riven already has is replaced, and nothing is deleted.

When a torrent Riven uses disappears from the account, the items using it are
reset, and another account torrent covering them is preferred over a new
scrape. That makes "add the new torrent, delete the old one" a swap.

Only torrents added after the import was first turned on count, and never
ones Riven itself used or added (leftovers from re-sourcing would bypass the
ranking rules). Torrents that can't be matched confidently are logged and, once per torrent,
sent to the configured Apprise URLs.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from apprise import Apprise
from kink import di
from loguru import logger
from RTN import normalize_title, parse
from sqlalchemy import select
from sqlalchemy.orm import Session

from program.apis.tmdb_api import TMDBApi
from program.db import db_functions
from program.db.db import db_session
from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.media_entry import MediaEntry
from program.media.state import States
from program.services.downloaders.models import DownloadedTorrent
from program.services.downloaders.torbox import TorBoxNotFound
from program.settings import settings_manager
from program.types import Event
from program.utils import data_dir_path

if TYPE_CHECKING:
    from program.services.downloaders.torbox import TorBoxDownloader

# More missing torrents than this in one check looks like an API problem, not deletions.
MAX_REMOVED_PER_RUN = 10

DONE_STATES = (States.Completed, States.Symlinked, States.Downloaded)

EVENT_SOURCE = "TorBoxImport"


def _norm(title: str) -> str:
    """RTN's normalized title with runs of spaces collapsed ("vs. Acme")."""

    return " ".join(normalize_title(title).split())


@dataclass
class Match:
    kind: Literal["movie", "show"]
    title: str
    item_id: int | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None


class TorBoxAccountImport:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._matches = dict[str, Match | None]()
        self._pending_events = set[int]()
        self._state_file = data_dir_path / "torbox_import.json"
        self._known = set[str]()
        self._notified = set[str]()
        self._loaded = False

    @property
    def settings(self):
        return settings_manager.settings.downloaders.torbox.account_import

    # --- entry points ---

    def run(self) -> None:
        """Scheduled check of the account."""

        if not self._lock.acquire(blocking=False):
            return

        try:
            self._run()
        except Exception as e:
            logger.warning(f"TorBox import: check failed: {e}")
        finally:
            self._lock.release()

    def offer(self, item_id: int) -> bool:
        """
        Fill a just-reset item from the account before Riven re-scrapes it.

        Returns True if the item got a file from an account torrent.
        """

        if not self.settings.enabled:
            return False

        if not self._lock.acquire(timeout=30):
            return False

        try:
            self._run()

            with db_session() as session:
                item = session.get(MediaItem, item_id)
                return bool(item and item.filesystem_entry)
        except Exception as e:
            logger.warning(f"TorBox import: could not offer item {item_id}: {e}")
            return False
        finally:
            self._lock.release()

    # --- one check ---

    def _run(self) -> None:
        torbox = self._torbox()

        if torbox is None:
            return

        self._load_state()

        on_account = {
            str(t["id"]): t for t in torbox.list_torrents() if t.get("id") is not None
        }
        referenced = self._referenced()
        created = torbox.created_ids()

        if not self._state_file.exists():
            # First run: everything already on the account counts as Riven's
            self._known = set(on_account)

        # Torrents Riven used or added are never imported: leftovers from
        # re-sourcing skip the ranking rules
        self._known = (self._known | set(referenced) | created) & set(on_account)
        self._notified &= set(on_account)
        self._matches = {k: v for k, v in self._matches.items() if k in on_account}

        candidates = [
            t
            for tid, t in on_account.items()
            if tid not in self._known and t.get("download_finished")
        ]

        for torrent in candidates:
            tid = str(torrent["id"])

            if tid not in self._matches:
                try:
                    self._matches[tid] = self._resolve(torrent)
                except Exception as e:
                    logger.debug(f"TorBox import: lookup for torrent {tid} failed: {e}")

        reset_ids = self._handle_removed(torbox, referenced, on_account)

        filled = set[int]()

        for torrent in candidates:
            match = self._matches.get(str(torrent["id"]))

            if match and (item_id := self._fill(torbox, torrent, match)):
                filled.add(item_id)

        self._pending_events |= filled | reset_ids
        self._emit_pending()
        self._save_state()

    # --- matching ---

    def _resolve(self, torrent: dict[str, Any]) -> Match | None:
        name = str(torrent.get("name") or "")

        try:
            data = parse(name)
        except Exception:
            return self._unmatched(torrent, "name could not be parsed")

        title = data.parsed_title
        if not title:
            return self._unmatched(torrent, "no title in the name")

        kind: Literal["movie", "show"] = (
            "show" if data.seasons or data.episodes else "movie"
        )
        year = data.year or None

        # Library first (exact title or alias, then either half of "Title:
        # Subtitle"), so swaps never create a second copy of a title
        match = self._match_library(kind, title, year, exact=True)

        if match is None:
            match = self._match_library(kind, title, year, exact=False)

        if match is None:
            match = self._match_tmdb(kind, title, year)  # errors are retried next check

        if isinstance(match, str):
            return self._unmatched(torrent, match)

        logger.debug(f"TorBox import: '{name}' is {kind} '{match.title}'")
        return match

    def _match_library(
        self, kind: str, title: str, year: int | None, exact: bool
    ) -> Match | str | None:
        wanted = _norm(title)

        with db_session() as session:
            rows = session.execute(
                select(
                    MediaItem.id, MediaItem.title, MediaItem.aired_at, MediaItem.aliases
                ).where(MediaItem.type == kind)
            ).all()

        def names(row: Any) -> set[str]:
            if not exact:
                return {_norm(part) for part in row.title.split(":") if part.strip()}

            aliases = [a for values in (row.aliases or {}).values() for a in values]
            return {_norm(n) for n in [row.title, *aliases] if n}

        hits = [r for r in rows if r.title and wanted in names(r)]

        if year:
            near = [r for r in hits if r.aired_at and abs(r.aired_at.year - year) <= 1]
            exact_year = [r for r in near if r.aired_at.year == year]

            # Show names often carry the season's year instead of the show's
            if near or kind == "movie":
                hits = exact_year if len(exact_year) == 1 else near

        if len(hits) == 1:
            return Match(kind=kind, title=hits[0].title, item_id=hits[0].id)  # type: ignore[arg-type]

        if len(hits) > 1:
            return f"{len(hits)} titles in the library match '{title}'"

        return None

    def _match_tmdb(self, kind: str, title: str, year: int | None) -> Match | str:
        tmdb = di[TMDBApi]
        tmdb_kind = "movie" if kind == "movie" else "tv"
        wanted = _norm(title)

        results = tmdb.search(tmdb_kind, title, year)

        if year and not results:
            # Release years in names are sometimes off by one
            results = tmdb.search(tmdb_kind, title)

        def names(r: dict[str, Any]) -> set[str]:
            keys = ("title", "original_title", "name", "original_name")
            return {_norm(str(r[k])) for k in keys if r.get(k)}

        def first_year(r: dict[str, Any]) -> int | None:
            date = r.get("release_date") or r.get("first_air_date") or ""
            return int(date[:4]) if date[:4].isdigit() else None

        hits = [r for r in results if wanted in names(r)]

        if year:
            near = [r for r in hits if (y := first_year(r)) and abs(y - year) <= 1]
            exact = [r for r in near if first_year(r) == year]
            hits = exact if exact else near

        if not hits:
            return f"no TMDB {tmdb_kind} called '{title}'" + (
                f" ({year})" if year else ""
            )

        hits.sort(key=lambda r: r.get("popularity") or 0, reverse=True)

        if len(hits) > 1 and (hits[0].get("popularity") or 0) < 3 * (
            hits[1].get("popularity") or 0
        ):
            return f"{len(hits)} TMDB titles match '{title}'" + (
                "" if year else "; a year in the name would help"
            )

        best = hits[0]
        label = str(best.get("title") or best.get("name") or title)

        if kind == "movie":
            return Match(kind="movie", title=label, tmdb_id=str(best["id"]))

        tvdb_id = tmdb.get_tvdb_id(best["id"])

        if not tvdb_id:
            return f"TMDB lists no TVDB id for '{label}'"

        return Match(kind="show", title=label, tmdb_id=str(best["id"]), tvdb_id=tvdb_id)

    def _unmatched(self, torrent: dict[str, Any], reason: str) -> None:
        tid = str(torrent["id"])
        name = torrent.get("name") or tid

        logger.warning(f"TorBox import: couldn't match '{name}': {reason}")

        if tid not in self._notified:
            self._notified.add(tid)
            self._notify(
                f'Couldn\'t match TorBox torrent "{name}" to a title ({reason}). '
                "Link it in Riven with a manual magnet."
            )

    # --- linking ---

    def _fill(
        self, torbox: TorBoxDownloader, torrent: dict[str, Any], match: Match
    ) -> int | None:
        """Link the torrent's files to items without one; return the item id if any."""

        tid = str(torrent["id"])
        infohash = str(torrent.get("hash") or "").lower()

        with db_session() as session:
            item = self._item_for(session, match)

            if item is None:
                item = self._create(session, match)

                if item is None:
                    return None

            if self._busy(session, item.id) or not self._has_gap(item, torrent):
                return None

            item_type = "movie" if isinstance(item, Movie) else "show"
            container, reason, info = torbox._process_torrent(tid, infohash, item_type)

            if container is None or info is None:
                logger.debug(
                    f"TorBox import: can't use '{torrent.get('name')}': {reason}"
                )
                return None

            container.torrent_id = tid
            container.torrent_info = info

            result = DownloadedTorrent(
                id=tid, infohash=infohash, container=container, info=info
            )
            processed = set[str]()
            downloader = self._program().services.downloader

            if not downloader.update_item_attributes(item, result, torbox, processed):
                session.rollback()
                return None

            if isinstance(item, Movie):
                MediaItem.store_state(item, States.Downloaded)
            else:
                for season in item.seasons:
                    for episode in season.episodes:
                        if str(episode.id) in processed:
                            MediaItem.store_state(episode, States.Downloaded)
                    MediaItem.store_state(season)
                MediaItem.store_state(item)

            session.commit()

            count = 1 if isinstance(item, Movie) else len(processed)
            logger.info(
                f"TorBox import: linked '{torrent.get('name')}' to {item.log_string} "
                f"({count} {'file' if count == 1 else 'episodes'})"
            )

            return item.id

    def _item_for(self, session: Session, match: Match) -> Movie | Show | None:
        item = session.get(MediaItem, match.item_id) if match.item_id else None

        if item is None and (match.tmdb_id or match.tvdb_id):
            try:
                item = db_functions.get_item_by_external_id(
                    tmdb_id=match.tmdb_id if match.kind == "movie" else None,
                    tvdb_id=match.tvdb_id,
                    session=session,
                )
            except ValueError:
                item = None

        if isinstance(item, (Season, Episode)):
            item = item.top_parent

        if isinstance(item, (Movie, Show)):
            match.item_id = item.id
            return item

        return None

    def _create(self, session: Session, match: Match) -> Movie | Show | None:
        ids = (
            {"tmdb_id": match.tmdb_id}
            if match.kind == "movie"
            else {"tvdb_id": match.tvdb_id}
        )
        prepared = MediaItem(
            {**ids, "requested_by": "torbox_import", "requested_at": datetime.now()}
        )

        result = next(self._program().services.indexer.run(prepared), None)

        if not result or not result.media_items:
            logger.warning(f"TorBox import: couldn't index '{match.title}'")
            return None

        item = result.media_items[0]
        item.store_state()
        item = session.merge(item)
        session.commit()

        logger.info(f"TorBox import: added {item.log_string} to Riven")

        if not isinstance(item, (Movie, Show)):
            return None

        match.item_id = item.id
        return item

    @staticmethod
    def _has_gap(item: Movie | Show, torrent: dict[str, Any]) -> bool:
        infohash = str(torrent.get("hash") or "").lower()

        if isinstance(item, Movie):
            return (
                not item.filesystem_entry
                and item.last_state not in DONE_STATES
                and not any(s.infohash == infohash for s in item.blacklisted_streams)
            )

        for file in torrent.get("files") or []:
            try:
                data = parse(str(file.get("short_name") or file.get("name") or ""))
            except Exception as e:
                logger.debug(f"TorBox import: can't parse file name: {e}")
                continue

            season = data.seasons[0] if data.seasons else None

            for number in data.episodes:
                episode = item.get_absolute_episode(number, season)

                if (
                    episode
                    and not episode.filesystem_entry
                    and episode.last_state not in DONE_STATES
                    and not any(
                        s.infohash == infohash for s in episode.blacklisted_streams
                    )
                ):
                    return True

        return False

    def _busy(self, session: Session, item_id: int) -> bool:
        em = self._program().em
        top, related = db_functions.get_item_ids(session, item_id)

        return any(
            em._id_in_queue(i) or em._id_in_running_events(i)
            for i in [top, *related]
            if i
        )

    # --- torrents removed from the account ---

    def _handle_removed(
        self,
        torbox: TorBoxDownloader,
        referenced: dict[str, set[int]],
        on_account: dict[str, Any],
    ) -> set[int]:
        missing = [tid for tid in referenced if tid not in on_account]

        if not missing:
            return set()

        if len(missing) > MAX_REMOVED_PER_RUN:
            message = (
                f"{len(missing)} torrents Riven uses are missing from TorBox at once; "
                "not resetting anything (probably a TorBox API problem)"
            )
            logger.warning(f"TorBox import: {message}")
            self._notify(message)
            return set()

        gone = list[str]()

        for tid in missing:
            try:
                torbox.get_torrent_info(tid)
            except TorBoxNotFound:
                gone.append(tid)
            except Exception as e:
                logger.debug(
                    f"TorBox import: couldn't confirm torrent {tid} is gone: {e}"
                )

        if not gone:
            return set()

        from routers.secure.items import apply_item_mutation

        reset = set[int]()

        with db_session() as session:
            for tid in gone:
                for item_id in referenced[tid]:
                    item = session.get(MediaItem, item_id)

                    if item is None:
                        continue

                    def mutation(i: MediaItem, s: Session) -> None:  # noqa: ARG001
                        i.blacklist_active_stream()
                        i.reset(keep_vfs=True)

                    apply_item_mutation(
                        program=self._program(),
                        item=item,
                        mutation_fn=mutation,
                        session=session,
                    )
                    reset.add(item_id)

                logger.info(
                    f"TorBox import: torrent {tid} was removed from TorBox; "
                    f"finding new sources for {len(referenced[tid])} item(s)"
                )

            session.commit()

        return reset

    # --- helpers ---

    @staticmethod
    def _program():
        from program.program import Program

        return di[Program]

    def _torbox(self) -> TorBoxDownloader | None:
        if not self.settings.enabled:
            return None

        services = self._program().services

        if not services:
            return None

        return next(
            (
                s
                for s in services.downloader.services.values()
                if s.key == "torbox" and s.initialized
            ),
            None,
        )

    @staticmethod
    def _referenced() -> dict[str, set[int]]:
        """Torrent id -> ids of the items whose files come from it."""

        with db_session() as session:
            rows = session.execute(
                select(MediaEntry.provider_download_id, MediaEntry.media_item_id).where(
                    MediaEntry.provider == "torbox",
                    MediaEntry.provider_download_id.is_not(None),
                )
            ).all()

        referenced = dict[str, set[int]]()

        for torrent_id, item_id in rows:
            ids = referenced.setdefault(str(torrent_id), set())

            if item_id:
                ids.add(item_id)

        return referenced

    def _emit_pending(self) -> None:
        em = self._program().em

        for item_id in list(self._pending_events):
            if em.add_event(Event(EVENT_SOURCE, item_id)):
                self._pending_events.discard(item_id)

    def _notify(self, message: str) -> None:
        urls = self.settings.notify_urls

        if not urls:
            return

        try:
            apprise = Apprise()

            for url in urls:
                apprise.add(url)

            apprise.notify(title="Riven: TorBox import", body=message)
        except Exception as e:
            logger.warning(f"TorBox import: notification failed: {e}")

    def _load_state(self) -> None:
        if self._loaded:
            return

        self._loaded = True

        try:
            state = json.loads(self._state_file.read_text())
            self._known = set(map(str, state.get("known", [])))
            self._notified = set(map(str, state.get("notified", [])))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(
                f"TorBox import: ignoring unreadable {self._state_file}: {e}"
            )

    def _save_state(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps(
                    {"known": sorted(self._known), "notified": sorted(self._notified)}
                )
            )
        except Exception as e:
            logger.warning(f"TorBox import: couldn't save {self._state_file}: {e}")


account_import = TorBoxAccountImport()


def torbox_account_import() -> None:
    """Scheduler entry point."""

    account_import.run()
