"""Plex Updater module"""

import os
import re
import threading
import time

from kink import di
from loguru import logger
from plexapi.exceptions import BadRequest, Unauthorized
from plexapi.library import LibrarySection
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.exceptions import MaxRetryError, NewConnectionError, RequestError

from program.apis.plex_api import PlexAPI
from program.services.updaters.base import BaseUpdater
from program.settings import settings_manager

# Removing replaced versions: wait this long after an update, then for Plex to
# finish scanning (at most SCAN_WAIT seconds). More trashed items than
# MAX_TRASH in a section means the share is probably down, so nothing is removed.
CLEANUP_DELAY = 60
SCAN_WAIT = 15 * 60
MAX_TRASH = 10
PLEX_TYPES = {"movie": 1, "show": 4}


class PlexUpdater(BaseUpdater):
    def __init__(self):
        super().__init__("plexupdater")
        self.library_path = settings_manager.settings.updaters.library_path
        self.settings = settings_manager.settings.updaters.plex
        self.api = None
        self.sections = dict[LibrarySection, list[str]]()
        self._cleanup_lock = threading.Lock()
        self._cleanup_paths = set[str]()
        self._cleanup_timer: threading.Timer | None = None
        self._initialize()

    def validate(self) -> bool:  # noqa: C901
        """Validate Plex library"""

        if not self.settings.enabled:
            return False

        if not self.settings.token:
            logger.error("Plex token is not set!")
            return False

        if not self.settings.url:
            logger.error("Plex URL is not set!")
            return False

        if not self.library_path:
            logger.error("Library path is not set!")
            return False

        try:
            self.api = di[PlexAPI]
            self.api.validate_server()
            self.sections = self.api.map_sections_with_paths()
            self.initialized = True

            return True
        except Unauthorized as e:
            logger.error(f"Plex is not authorized!: {e}")
        except TimeoutError as e:
            logger.exception(f"Plex timeout error: {e}")
        except BadRequest as e:
            logger.exception(f"Plex is not configured correctly!: {e}")
        except MaxRetryError as e:
            logger.exception(f"Plex max retries exceeded: {e}")
        except NewConnectionError as e:
            logger.exception(f"Plex new connection error: {e}")
        except RequestsConnectionError as e:
            logger.exception(f"Plex requests connection error: {e}")
        except RequestError as e:
            logger.exception(f"Plex request error: {e}")
        except Exception as e:
            logger.exception(f"Plex exception thrown: {e}")
        return False

    def refresh_path(self, path: str) -> bool:
        """Refresh a specific path in Plex by finding the matching section"""

        assert self.api is not None, "Plex API is not initialized"

        path = map_path(path, self.settings.path_mappings)
        windows = is_windows_path(path)

        for section, section_paths in self.sections.items():
            for section_path in section_paths:
                if windows:
                    # Windows paths are case-insensitive.
                    matched = path.lower().startswith(section_path.lower())
                else:
                    matched = path.startswith(section_path)
                if matched:
                    return self.api.update_section(section, path)

        return False

    def _section_for(self, path: str) -> LibrarySection | None:
        """The Plex section holding this Plex-side path."""

        for section, section_paths in self.sections.items():
            for section_path in section_paths:
                if path_key(path).startswith(path_key(section_path)):
                    return section

        return None

    def schedule_version_cleanup(self, paths: list[str]) -> None:
        """
        Queue Riven files that were just refreshed in Plex. Once Plex has
        scanned them, old versions they replaced are removed from Plex's trash
        (``remove_replaced_versions`` setting).
        """

        if not (self.settings.remove_replaced_versions and self.initialized):
            return

        with self._cleanup_lock:
            self._cleanup_paths.update(paths)

            if self._cleanup_timer is None:
                self._cleanup_timer = threading.Timer(CLEANUP_DELAY, self._run_cleanup)
                self._cleanup_timer.daemon = True
                self._cleanup_timer.start()

    def _run_cleanup(self) -> None:
        with self._cleanup_lock:
            paths, self._cleanup_paths = self._cleanup_paths, set[str]()
            self._cleanup_timer = None

        try:
            self.remove_replaced_versions(sorted(paths))
        except Exception as e:
            logger.error(f"Plex: removing replaced versions failed: {e}")

    def _scanning(self, keys: set[str]) -> bool:
        assert self.api is not None and self.api.plex_server is not None

        return any(
            directory.get("key") in keys and directory.get("refreshing") == "1"
            for directory in self.api.plex_server.query("/library/sections")
        )

    def remove_replaced_versions(
        self, paths: list[str], dry_run: bool = False, wait: bool = True
    ) -> list[str]:
        """
        Remove versions of these Riven files' Plex items that Plex keeps as
        unavailable because Riven replaced their file. See ``replaced_versions``
        for what counts. Returns a description of each version removed (or, with
        ``dry_run``, that would be).
        """

        assert self.api is not None and self.api.plex_server is not None, (
            "Plex API is not initialized"
        )

        server = self.api.plex_server
        mappings = self.settings.path_mappings
        wanted = {path_key(map_path(path, mappings)) for path in paths}
        sections = {
            section
            for path in wanted
            if (section := self._section_for(path)) and section.type in PLEX_TYPES
        }

        if not sections:
            return []

        keys = {str(section.key) for section in sections}
        deadline = time.monotonic() + SCAN_WAIT

        while wait and time.monotonic() < deadline and self._scanning(keys):
            time.sleep(10)

        def riven_path(plex_path: str) -> str | None:
            return library_file(plex_path, mappings, str(self.library_path))

        removed = list[str]()

        for section in sections:
            trash = list(
                server.query(
                    f"/library/sections/{section.key}/all"
                    f"?type={PLEX_TYPES[section.type]}&trash=1"
                )
            )

            if len(trash) > MAX_TRASH:
                logger.warning(
                    f"Plex: {len(trash)} items in {section.title}'s trash (share down?); "
                    "not removing replaced versions"
                )
                continue

            for entry in trash:
                rating_key = entry.get("ratingKey")
                video = next(
                    iter(server.query(f"/library/metadata/{rating_key}?checkFiles=1")),
                    None,
                )

                if video is None:
                    continue

                for media_id, files in replaced_versions(
                    video, wanted, riven_path, os.path.exists
                ):
                    title = video.get("title")

                    if video.get("grandparentTitle"):
                        title = (
                            f"{video.get('grandparentTitle')} "
                            f"S{int(video.get('parentIndex') or 0):02d}"
                            f"E{int(video.get('index') or 0):02d}"
                        )

                    what = f"{title}: {', '.join(files)}"

                    if not dry_run:
                        server.query(
                            f"/library/metadata/{rating_key}/media/{media_id}",
                            method=server._session.delete,
                        )

                    logger.info(
                        f"Plex: {'would remove' if dry_run else 'removed'} "
                        f"replaced version {what}"
                    )
                    removed.append(what)

        return removed

    def sessions_playing(self, paths: list[str]) -> list:
        """Plex sessions playing any of these Riven paths."""

        if not self.api or not self.api.plex_server:
            return []

        return playing_sessions(
            self.api.plex_server.sessions(),
            paths,
            self.settings.path_mappings,
            fetch_item=self.api.plex_server.fetchItem,
        )


def playing_sessions(
    sessions, paths: list[str], mappings: dict[str, str], *, fetch_item=None
) -> list:
    """Match source files, retaining the live sessions needed by ``stop()``.

    Transcoded sessions can omit Part.file. Resolve those parts from library
    metadata by part ID, never by title or by an arbitrary alternate version.
    Metadata is cached only for this call so replacements cannot leave stale paths.
    """

    def key(path: str) -> str:
        return path.lower() if is_windows_path(path) else path

    wanted = {key(map_path(path, mappings)) for path in paths}
    metadata = {}
    found = []

    for session in sessions:
        parts = [part for media in session.media for part in media.parts]
        source_paths = [part.file for part in parts if part.file]
        missing = [part for part in parts if not part.file]
        rating_key = getattr(session, "ratingKey", None)

        if missing and fetch_item and rating_key is not None:
            cache_key = str(rating_key)
            if cache_key not in metadata:
                try:
                    metadata[cache_key] = fetch_item(rating_key)
                except Exception as exc:
                    # Do not let one stale/inaccessible item hide other sessions.
                    # Exception text can contain credentials or request URLs.
                    logger.warning(
                        f"Plex playback: cannot resolve source for item {cache_key} "
                        f"({type(exc).__name__})"
                    )
                    metadata[cache_key] = None

            item = metadata[cache_key]
            library_parts = [
                part
                for media in getattr(item, "media", [])
                for part in media.parts
                if part.file
            ]
            for part in missing:
                part_id = getattr(part, "id", None)
                if part_id is not None:
                    source_paths.extend(
                        candidate.file
                        for candidate in library_parts
                        if str(getattr(candidate, "id", None)) == str(part_id)
                    )
                elif len(library_parts) == 1:
                    # No ID: only an unambiguous single source is safe to use.
                    source_paths.append(library_parts[0].file)

        if any(key(path) in wanted for path in source_paths):
            found.append(session)

    return found


def replaced_versions(video, wanted: set[str], riven_path, exists) -> list:
    """
    Versions of a Plex item (``Video`` element from ``checkFiles=1``) that
    Riven replaced, as ``(media_id, files)``.

    A version counts only if Plex marked it deleted and finds none of its
    files, each of its files maps into Riven's library and is gone there too,
    and the item has a present version with one of the ``wanted`` (Plex-side
    key) files, which exists in Riven's library. Plex deletes the file of a
    version it removes, so anything less certain is left for Empty Trash.
    """

    present = list[str]()
    gone = list[tuple[str, list[str]]]()

    for media in video.findall("Media"):
        parts = media.findall("Part")
        files = [part.get("file") or "" for part in parts]

        if not parts or not all(files):
            continue

        if media.get("deletedAt") and all(
            part.get("exists") == "0" and part.get("accessible") == "0"
            for part in parts
        ):
            gone.append((str(media.get("id")), files))
        elif not media.get("deletedAt") and all(
            part.get("exists") == "1" for part in parts
        ):
            present.extend(files)

    def in_riven(plex_path: str) -> bool:
        local = riven_path(plex_path)
        return bool(local and exists(local))

    if not any(path_key(f) in wanted and in_riven(f) for f in present):
        return []

    replaced = []

    for media_id, files in gone:
        local = [riven_path(f) for f in files]

        if media_id != "None" and all(local) and not any(exists(p) for p in local):
            replaced.append((media_id, files))

    return replaced


def library_file(plex_path: str, mappings: dict[str, str], library_path: str) -> str | None:
    """The Riven path of a Plex-side file (``map_path`` reversed), or None if it
    isn't in Riven's library."""

    path = plex_path

    for source in sorted(mappings, key=lambda s: len(mappings[s]), reverse=True):
        target = mappings[source]

        if is_windows_path(target):
            prefix = target.rstrip("\\")

            if path_key(plex_path) == path_key(prefix) or path_key(plex_path).startswith(
                path_key(prefix) + "\\"
            ):
                path = source.rstrip("/") + plex_path[len(prefix) :].replace("\\", "/")
                break
        else:
            prefix = target.rstrip("/")

            if plex_path == prefix or plex_path.startswith(prefix + "/"):
                path = source.rstrip("/") + plex_path[len(prefix) :]
                break

    library = library_path.rstrip("/")

    if is_windows_path(path) or not path.startswith(library + "/"):
        return None

    return path


def path_key(path: str) -> str:
    """Comparable form of a Plex path (Windows paths are case-insensitive)."""

    return path.lower() if is_windows_path(path) else path


def is_windows_path(path: str) -> bool:
    return path.startswith("\\\\") or re.match(r"^[A-Za-z]:[\\\\/]", path) is not None


def map_path(path: str, mappings: dict[str, str]) -> str:
    """Translate a Riven path into the path Plex sees, using the longest matching prefix.

    A Windows target (`\\\\server\\share` or `D:\\media`) also gets backslashes,
    so `/mnt/debrid/riven/shows/X` becomes `\\\\wsl-media\\riven\\shows\\X`.
    """

    for source in sorted(mappings, key=len, reverse=True):
        prefix = source.rstrip("/")
        if path != prefix and not path.startswith(prefix + "/"):
            continue

        target = mappings[source]
        rest = path[len(prefix) :]

        if is_windows_path(target):
            return target.rstrip("\\") + rest.replace("/", "\\")

        return target.rstrip("/") + rest

    return path
