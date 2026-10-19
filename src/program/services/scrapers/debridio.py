"""Debridio scraper module (TorBox only)

Debridio's Stremio addon returns playback URLs instead of infohashes, and its
titles are file names, so a season pack looks like a single episode. The hash
is a path segment of every URL; real torrent names come from TorBox's free
`checkcached` endpoint, which also drops anything TorBox has not cached.
Those calls go through the TorBox downloader, so both share one rate limit
and one 429 pause.
"""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING, Any

from kink import di
from loguru import logger
from pydantic import BaseModel

from program.media.item import MediaItem
from program.services.scrapers.base import ScraperService
from program.services.scrapers.languages import flag_languages, with_languages
from program.settings import settings_manager
from program.settings.models import DebridioConfig
from program.utils.request import CircuitBreakerOpen, SmartSession

if TYPE_CHECKING:
    from program.services.downloaders.torbox import TorBoxDownloader

CHECKCACHED_BATCH = 100
# Most results are kept, but a huge movie answer (600+ streams) should not
# cost more than a few TorBox calls.
MAX_HASHES = 300

HASH_IN_URL = re.compile(r"/([0-9a-fA-F]{40})(?:/|$)")
SINGLE_EPISODE = re.compile(r"\bS(\d{1,2})[ ._-]?E\d{1,4}\b", re.IGNORECASE)
SEASON_IN_PATH = re.compile(
    r"\bS(\d{1,2})[ ._-]?E\d{1,4}\b|\bSeason[ ._-]?(\d{1,2})\b", re.IGNORECASE
)
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")
# Uploader tag TorBox keeps at the end of single-file names, e.g.
# `Show.S03E08.1080p.WEB.h264-ETHEL[EZTVx.to].mkv`. RTN reads it as a site
# and drops the release when `extras.site` is off.
UPLOADER_TAG = re.compile(
    r"\s*\[(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,6}\](?=(?:\.\w{2,4})?$)",
    re.IGNORECASE,
)


class DebridioScrapeResponse(BaseModel):
    """Model for Debridio scrape response"""

    class Stream(BaseModel):
        title: str = ""
        url: str = ""

    streams: list[Stream] = []


def hash_from_url(url: str) -> str | None:
    match = HASH_IN_URL.search(url)
    return match.group(1).lower() if match else None


def language_tags(debridio_title: str) -> str:
    """Language words from Debridio's "🌐 Multi Audio|🇫🇷" line, e.g. "MULTI FRENCH"."""

    lines = [line for line in debridio_title.split("\n")[1:] if "🌐" in line]
    return " ".join(dict.fromkeys(" ".join(map(flag_languages, lines)).split()))


def torrent_title(name: str, files: list[dict[str, Any]]) -> str:
    """Return a title Riven can rank for this torrent.

    TorBox sometimes names a multi-episode torrent after one of its files
    (e.g. `Show.S05E01.Title.1080p.mkv` for a 153-file series pack). Riven
    would then treat the pack as a single episode, so the episode token is
    replaced with the season range found in the file list. A trailing
    uploader tag like `[EZTVx.to]` is removed.
    """

    name = UPLOADER_TAG.sub("", name)
    videos = [
        f.get("name", "")
        for f in files
        if f.get("name", "").lower().endswith(VIDEO_EXTENSIONS)
    ]
    match = SINGLE_EPISODE.search(name)

    if len(videos) <= 1 or not match:
        return name

    seasons = set[int]()
    for path in videos:
        for found in SEASON_IN_PATH.finditer(path):
            seasons.add(int(found.group(1) or found.group(2)))

    if not seasons:
        return name

    low, high = min(seasons), max(seasons)
    season_range = f"S{low:02d}" if low == high else f"S{low:02d}-S{high:02d}"
    title = name[: match.start()] + season_range + name[match.end() :]

    if title.lower().endswith(VIDEO_EXTENSIONS):
        title = title.rsplit(".", 1)[0]

    return title


class Debridio(ScraperService[DebridioConfig]):
    """Scraper for `Debridio`, resolved through TorBox"""

    requires_imdb_id = True

    def __init__(self):
        super().__init__()

        self.settings = settings_manager.settings.scraping.debridio
        self.timeout = self.settings.timeout or 30

        self.session = SmartSession(
            rate_limits=(
                {
                    "addon.debridio.com": {"rate": 60 / 60, "capacity": 30},
                }
                if self.settings.ratelimit
                else None
            ),
            retries=self.settings.retries,
            backoff_factor=0.3,
        )
        self.headers = {"User-Agent": "Mozilla/5.0"}
        self.proxies = (
            {"http": self.settings.proxy_url, "https": self.settings.proxy_url}
            if self.settings.proxy_url
            else None
        )

        self._initialize()

    @property
    def torbox_key(self) -> str:
        return settings_manager.settings.downloaders.torbox.api_key

    def _config_segment(self) -> str:
        """Base64 config path segment, in the format Debridio's own page builds."""

        config = {
            "api_key": self.settings.api_key,
            "provider": "torbox",
            "providerKey": self.torbox_key,
            "disableUncached": False,
            "maxSize": "",
            "maxReturnPerQuality": "",
            "resolutions": ["4k", "1440p", "1080p", "720p", "480p", "360p", "unknown"],
            "excludedQualities": [],
        }

        return base64.b64encode(json.dumps(config).encode()).decode()

    @staticmethod
    def _status_message(status: int) -> str:
        if status in (401, 403):
            # Debridio answers a wrong key with 403 "Subscription is inactive".
            return f"Debridio rejected the API key or the subscription is inactive ({status})"
        return f"Debridio request failed - Status Code: {status}"

    def validate(self) -> bool:
        """Validate the Debridio settings."""

        if not self.settings.enabled:
            return False

        if not self.settings.url or not self.settings.api_key:
            logger.error("Debridio URL or API key is not configured.")
            return False

        if not self.torbox_key:
            logger.error("Debridio needs the TorBox downloader's API key.")
            return False

        try:
            response = self.session.get(
                f"{self.settings.url.rstrip('/')}/{self._config_segment()}/manifest.json",
                timeout=10,
                headers=self.headers,
                proxies=self.proxies,
            )

            if not response.ok:
                # Never log the URL: it embeds both API keys.
                logger.error(self._status_message(response.status_code))

            return response.ok
        except Exception as e:
            logger.error(f"Debridio failed to initialize: {type(e).__name__}")

            return False

    def run(self, item: MediaItem) -> dict[str, str]:
        """Scrape Debridio with the given media item for streams"""

        try:
            return self.scrape(item)
        except Exception as e:
            # Exception text can contain the request URL, which embeds keys.
            logger.error(
                f"Debridio scrape failed for {item.log_string}: {type(e).__name__}"
            )

        return {}

    def scrape(self, item: MediaItem) -> dict[str, str]:
        """Wrapper for `Debridio` scrape method"""

        identifier, scrape_type, imdb_id = self.get_stremio_identifier(item)

        if not imdb_id:
            return {}

        url = (
            f"{self.settings.url.rstrip('/')}/{self._config_segment()}"
            f"/stream/{scrape_type}/{imdb_id}{identifier or ''}.json"
        )

        response = self.session.get(
            url, timeout=self.timeout, headers=self.headers, proxies=self.proxies
        )

        if not response.ok:
            logger.error(
                f"{self._status_message(response.status_code)} for {item.log_string}"
            )
            return {}

        data = DebridioScrapeResponse.model_validate(response.json())

        # Debridio's file-name titles are only a fallback if TorBox can't answer.
        fallback = dict[str, str]()
        languages = dict[str, str]()
        for stream in data.streams:
            infohash = hash_from_url(stream.url)
            if infohash and infohash not in fallback:
                fallback[infohash] = stream.title.split("\n")[0]
                languages[infohash] = language_tags(stream.title)
            if len(fallback) >= MAX_HASHES:
                break

        if not fallback:
            logger.log("NOT_FOUND", f"No streams found for {item.log_string}")
            return {}

        cached = self._torbox_cached(list(fallback))

        if cached is None:
            torrents = fallback
        else:
            torrents = {
                infohash: torrent_title(
                    entry.get("name") or fallback[infohash], entry.get("files") or []
                )
                for infohash, entry in cached.items()
                if infohash in fallback
            }

        torrents = {
            infohash: with_languages(title, languages.get(infohash, ""))
            for infohash, title in torrents.items()
        }

        if torrents:
            logger.log(
                "SCRAPER", f"Found {len(torrents)} streams for {item.log_string}"
            )
        else:
            logger.log("NOT_FOUND", f"No cached streams found for {item.log_string}")

        return torrents

    @staticmethod
    def _torbox() -> TorBoxDownloader | None:
        """The running TorBox downloader, if any."""

        from program.program import Program  # noqa: PLC0415 - circular import

        services = di[Program].services

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

    def _torbox_cached(self, hashes: list[str]) -> dict[str, dict[str, Any]] | None:
        """TorBox entries for the cached hashes, or None if TorBox can't answer.

        Uses the downloader's session and 429 pause, so Debridio and the
        downloader stay under one TorBox rate limit together, and a 429 seen
        by either pauses both.
        """

        torbox = self._torbox()

        if torbox is None:
            logger.debug("TorBox downloader not running; using Debridio's titles")
            return None

        cached = dict[str, dict[str, Any]]()

        for start in range(0, len(hashes), CHECKCACHED_BATCH):
            batch = hashes[start : start + CHECKCACHED_BATCH]

            try:
                response, body = torbox._request(
                    "GET",
                    "torrents/checkcached",
                    params={
                        "hash": ",".join(batch),
                        "format": "object",
                        "list_files": "true",
                    },
                    timeout=self.timeout,
                )
            except CircuitBreakerOpen:
                # Throttled now or still paused: fall back rather than stall
                # the scrape. The downloader already logged the 429.
                logger.debug("TorBox checkcached paused; using Debridio's titles")
                return None
            except Exception as e:
                logger.warning(f"TorBox checkcached failed: {type(e).__name__}")
                return None

            if not response.ok:
                logger.warning(f"TorBox checkcached returned {response.status_code}")
                return None

            for infohash, entry in (body.get("data") or {}).items():
                cached[infohash.lower()] = entry or {}

        return cached
