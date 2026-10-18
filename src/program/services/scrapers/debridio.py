"""Debridio scraper module (TorBox only)

Debridio's Stremio addon returns playback URLs instead of infohashes, and its
titles are file names, so a season pack looks like a single episode. The hash
is a path segment of every URL; real torrent names come from TorBox's free
`checkcached` endpoint, which also drops anything TorBox has not cached.
"""

import base64
import json
import re
from typing import Any

from loguru import logger
from pydantic import BaseModel

from program.media.item import MediaItem
from program.services.scrapers.base import ScraperService
from program.settings import settings_manager
from program.settings.models import DebridioConfig
from program.utils.request import SmartSession

TORBOX_URL = "https://api.torbox.app/v1/api"
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

# Debridio puts languages on a "🌐 Multi Audio|🇫🇷" line as flags. TorBox names
# often lack them, so they are appended as words RTN recognises (uppercase:
# RTN does not match "French").
FLAG_LANGUAGES = {
    "GB": "ENGLISH",
    "US": "ENGLISH",
    "FR": "FRENCH",
    "ES": "SPANISH",
    "MX": "LATINO",
    "IT": "ITALIAN",
    "DE": "GERMAN",
    "RU": "RUSSIAN",
    "UA": "UKRAINIAN",
    "PL": "POLISH",
    "PT": "PORTUGUESE",
    "BR": "PORTUGUESE",
    "NL": "DUTCH",
    "SE": "SWEDISH",
    "NO": "NORWEGIAN",
    "DK": "DANISH",
    "FI": "FINNISH",
    "CZ": "CZECH",
    "SK": "SLOVAK",
    "HU": "HUNGARIAN",
    "RO": "ROMANIAN",
    "BG": "BULGARIAN",
    "GR": "GREEK",
    "TR": "TURKISH",
    "IL": "HEBREW",
    "SA": "ARABIC",
    "IR": "PERSIAN",
    "IN": "HINDI",
    "CN": "CHINESE",
    "TW": "CHINESE",
    "JP": "JAPANESE",
    "KR": "KOREAN",
    "TH": "THAI",
    "VN": "VIETNAMESE",
    "ID": "INDONESIAN",
    "RS": "SERBIAN",
    "HR": "CROATIAN",
    "SI": "SLOVENIAN",
    "LT": "LITHUANIAN",
    "LV": "LATVIAN",
    "EE": "ESTONIAN",
}


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
    """Language words from Debridio's flag line, e.g. "MULTI FRENCH"."""

    words = list[str]()

    for line in debridio_title.split("\n")[1:]:
        if "🌐" not in line:
            continue
        for raw in line.replace("🌐", "").split("|"):
            part = raw.strip()
            if part in ("Multi Audio", "Dual Audio"):
                words.append(part.split()[0].upper())
            # A flag is two regional-indicator characters.
            elif len(part) == 2 and all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in part):
                code = "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in part)
                if language := FLAG_LANGUAGES.get(code):
                    words.append(language)

    return " ".join(dict.fromkeys(words))


def torrent_title(name: str, files: list[dict[str, Any]]) -> str:
    """Return a title Riven can rank for this torrent.

    TorBox sometimes names a multi-episode torrent after one of its files
    (e.g. `Show.S05E01.Title.1080p.mkv` for a 153-file series pack). Riven
    would then treat the pack as a single episode, so the episode token is
    replaced with the season range found in the file list.
    """

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
        # retries=0: a TorBox 429 carries Retry-After: 300, which SmartSession
        # would sleep out inline. Fall back to Debridio's titles instead.
        # 60/min leaves most of TorBox's 300/min for the downloader.
        self.torbox_session = SmartSession(
            rate_limits={"api.torbox.app": {"rate": 60 / 60, "capacity": 10}},
            retries=0,
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
            infohash: (
                f"{title} [{languages[infohash]}]" if languages.get(infohash) else title
            )
            for infohash, title in torrents.items()
        }

        if torrents:
            logger.log(
                "SCRAPER", f"Found {len(torrents)} streams for {item.log_string}"
            )
        else:
            logger.log("NOT_FOUND", f"No cached streams found for {item.log_string}")

        return torrents

    def _torbox_cached(self, hashes: list[str]) -> dict[str, dict[str, Any]] | None:
        """TorBox entries for the cached hashes, or None if TorBox can't answer."""

        cached = dict[str, dict[str, Any]]()

        for start in range(0, len(hashes), CHECKCACHED_BATCH):
            batch = hashes[start : start + CHECKCACHED_BATCH]

            try:
                response = self.torbox_session.get(
                    f"{TORBOX_URL}/torrents/checkcached",
                    params={
                        "hash": ",".join(batch),
                        "format": "object",
                        "list_files": "true",
                    },
                    headers={
                        **self.headers,
                        "Authorization": f"Bearer {self.torbox_key}",
                    },
                    timeout=self.timeout,
                    proxies=self.proxies,
                )
            except Exception as e:
                logger.warning(f"TorBox checkcached failed: {type(e).__name__}")
                return None

            if not response.ok:
                # 429 included: fall back rather than stall the scrape.
                logger.warning(f"TorBox checkcached returned {response.status_code}")
                return None

            for infohash, entry in (response.json().get("data") or {}).items():
                cached[infohash.lower()] = entry or {}

        return cached
