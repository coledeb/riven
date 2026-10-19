"""Plex Updater module"""

import re

from kink import di
from loguru import logger
from plexapi.exceptions import BadRequest, Unauthorized
from plexapi.library import LibrarySection
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.exceptions import MaxRetryError, NewConnectionError, RequestError

from program.apis.plex_api import PlexAPI
from program.services.updaters.base import BaseUpdater
from program.settings import settings_manager


class PlexUpdater(BaseUpdater):
    def __init__(self):
        super().__init__("plexupdater")
        self.library_path = settings_manager.settings.updaters.library_path
        self.settings = settings_manager.settings.updaters.plex
        self.api = None
        self.sections = dict[LibrarySection, list[str]]()
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
