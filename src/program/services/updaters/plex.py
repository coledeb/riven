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
            self.api.plex_server.sessions(), paths, self.settings.path_mappings
        )


def playing_sessions(sessions, paths: list[str], mappings: dict[str, str]) -> list:
    """The Plex sessions whose file is one of these Riven paths (as Plex sees them)."""

    def key(path: str) -> str:
        return path.lower() if is_windows_path(path) else path

    wanted = {key(map_path(path, mappings)) for path in paths}

    return [
        session
        for session in sessions
        if any(
            key(part.file or "") in wanted
            for media in session.media
            for part in media.parts
        )
    ]


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
