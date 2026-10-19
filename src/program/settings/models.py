"""Riven settings models"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Annotated

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
    BeforeValidator,
)
from pydantic.networks import PostgresDsn
from RTN.models import SettingsModel

from program.settings.migratable import MigratableBaseModel
from program.utils import generate_api_key, get_version

deprecation_warning = (
    "This has been deprecated and will be removed in a future version."
)


def validate_empty_or_url(v: Any) -> str:
    if isinstance(v, str):
        if v == "":
            return v
        if not v.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
            raise ValueError("Must be a valid URL or empty string")
        return v
    raise ValueError("Must be a string")


EmptyOrUrl = Annotated[str, BeforeValidator(validate_empty_or_url)]


class Observable(MigratableBaseModel):
    class Config:
        arbitrary_types_allowed = True

    _notify_observers: Callable[..., Any] | None = None

    @classmethod
    def set_notify_observers(cls, notify_observers_callable: Callable[..., Any]):
        cls._notify_observers = notify_observers_callable

    def __setattr__(self, name: str, value: Any):
        super().__setattr__(name, value)

        if self.__class__._notify_observers:
            self.__class__._notify_observers()


# Download Services


class RealDebridModel(Observable):
    enabled: bool = Field(default=False, description="Enable Real-Debrid")
    api_key: str = Field(default="", description="Real-Debrid API key")


class DebridLinkModel(Observable):
    enabled: bool = Field(default=False, description="Enable Debrid-Link")
    api_key: str = Field(default="", description="Debrid-Link API key")


class AllDebridModel(Observable):
    enabled: bool = Field(default=False, description="Enable AllDebrid")
    api_key: str = Field(default="", description="AllDebrid API key")


class TorBoxImportModel(Observable):
    enabled: bool = Field(
        default=False,
        description=(
            "Link torrents added to the TorBox account outside Riven (e.g. with "
            "Debrid Media Manager) into the library. Fills missing movies and "
            "episodes (see replace_existing to also replace existing ones); deleting a torrent Riven uses makes Riven prefer another "
            "account torrent for it. Never deletes. Applies after a restart."
        ),
    )
    replace_existing: bool = Field(
        default=False,
        description=(
            "Also let a newly added account torrent replace the files Riven already "
            "has for the movie or episodes it contains (e.g. a better copy picked in "
            "Debrid Media Manager), even one Riven had blacklisted for them. Files "
            "stay in place and switch to the new torrent; one that is being played "
            "is switched after playback (or see stop_playback_to_replace). The old "
            "torrent is left on the account."
        ),
    )
    stop_playback_to_replace: bool = Field(
        default=False,
        description=(
            "With replace_existing: if the file being replaced is playing in Plex, "
            "stop that playback with the message below and replace it right away, "
            "instead of waiting until playback ends. Needs the Plex updater "
            "(stopping playback needs Plex Pass)."
        ),
    )
    stop_playback_message: str = Field(
        default=(
            "This video is being switched to a new copy. Press play again in a "
            "moment to continue where you left off."
        ),
        description="Message Plex shows when playback is stopped for a replace",
    )
    interval: int = Field(
        default=120, ge=30, description="Seconds between TorBox account checks"
    )
    notify_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Apprise URLs (e.g. a Discord webhook) told once when a torrent "
            "can't be matched to a title"
        ),
    )


class TorBoxModel(Observable):
    enabled: bool = Field(default=False, description="Enable TorBox")
    api_key: str = Field(default="", description="TorBox API key")
    only_manage_own_torrents: bool = Field(
        default=True,
        description=(
            "Only delete TorBox torrents that Riven added during this run. Turn off "
            "to also clean up failed torrents that were already on the account; "
            "torrents still used by Riven's library are always kept. Leave on if "
            "other tools share the TorBox account."
        ),
    )
    account_import: TorBoxImportModel = Field(
        default_factory=lambda: TorBoxImportModel(),
        description="Import torrents added to the TorBox account outside Riven",
    )


# File extensions the downloaders can match; anything else is ignored.
ALLOWED_VIDEO_EXTENSIONS = [
    "mp4",
    "mkv",
    "avi",
    "mov",
    "wmv",
    "flv",
    "m4v",
    "webm",
    "mpg",
    "mpeg",
    "m2ts",
    "ts",
]
DEFAULT_VIDEO_EXTENSIONS = ["mp4", "mkv", "avi", "m4v", "mov", "webm", "ts"]


class DownloadersModel(Observable):
    video_extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_VIDEO_EXTENSIONS),
        description="Video file extensions to consider for downloads (applied after a backend restart)",
        json_schema_extra={
            "uniqueItems": True,
            "items": {"type": "string", "enum": ALLOWED_VIDEO_EXTENSIONS},
        },
    )
    movie_filesize_mb_min: int = Field(
        default=700, ge=1, description="Minimum file size in MB for movies"
    )
    movie_filesize_mb_max: int = Field(
        default=-1,
        ge=-1,
        description="Maximum file size in MB for movies (-1 for no limit)",
    )
    episode_filesize_mb_min: int = Field(
        default=100, ge=1, description="Minimum file size in MB for episodes"
    )
    episode_filesize_mb_max: int = Field(
        default=-1,
        ge=-1,
        description="Maximum file size in MB for episodes (-1 for no limit)",
    )
    proxy_url: EmptyOrUrl = Field(
        default="", description="Proxy URL for downloaders (optional)"
    )
    real_debrid: RealDebridModel = Field(
        default_factory=lambda: RealDebridModel(),
        description="Real-Debrid downloader configuration",
    )
    debrid_link: DebridLinkModel = Field(
        default_factory=lambda: DebridLinkModel(),
        description="Debrid-Link downloader configuration",
    )
    all_debrid: AllDebridModel = Field(
        default_factory=lambda: AllDebridModel(),
        description="AllDebrid downloader configuration",
    )
    torbox: TorBoxModel = Field(
        default_factory=lambda: TorBoxModel(),
        description="TorBox downloader configuration",
    )


# Filesystem Service


class LibraryProfileFilterRules(BaseModel):
    """Filter rules for library profile matching (metadata-only)"""

    content_types: list[str] | None = Field(
        default=None,
        description="Media types to include (movie, show). None/omit = all types",
    )
    genres: list[str] | None = Field(
        default=None,
        description="Genres to include/exclude. Prefix with '!' to exclude. "
        "Examples: ['action', 'adventure'] = include these genres, "
        "['action', '!horror'] = include action but exclude horror. "
        "None/omit = no genre filter",
    )
    exclude_genres: list[str] | None = Field(
        default=None,
        description="DEPRECATED: Use genres with '!' prefix instead. "
        "This field is kept for backward compatibility and will be auto-migrated.",
    )
    min_year: int | None = Field(
        default=None,
        ge=1900,
        description="Minimum release year. None/omit = no minimum",
    )
    max_year: int | None = Field(
        default=None,
        ge=1900,
        description="Maximum release year. None/omit = no maximum",
    )
    is_anime: bool | None = Field(
        default=None, description="Filter by anime flag. None/omit = no anime filter"
    )
    networks: list[str] | None = Field(
        default=None,
        description="TV networks to include/exclude. Prefix with '!' to exclude. "
        "Examples: ['HBO', 'Netflix'], ['HBO', '!Fox']. None/omit = no network filter",
    )
    countries: list[str] | None = Field(
        default=None,
        description="Countries to include/exclude. Prefix with '!' to exclude. "
        "Examples: ['US', 'GB'], ['US', '!CN']. None/omit = no country filter",
    )
    languages: list[str] | None = Field(
        default=None,
        description="Languages to include/exclude. Prefix with '!' to exclude. "
        "Examples: ['en', 'es'], ['en', '!zh']. None/omit = no language filter",
    )
    min_rating: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description="Minimum rating (0-10 scale). None/omit = no minimum",
    )
    max_rating: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description="Maximum rating (0-10 scale). None/omit = no maximum",
    )
    content_ratings: list[str] | None = Field(
        default=None,
        description="Content ratings to include/exclude. Prefix with '!' to exclude. "
        "Examples: ['PG', 'PG-13'], ['PG', '!R']. "
        "Common ratings: G, PG, PG-13, R, NC-17, TV-Y, TV-PG, TV-14, TV-MA. "
        "None/omit = no rating filter",
    )

    @model_validator(mode="after")
    def migrate_exclude_genres(self):
        """Auto-migrate exclude_genres to genres with '!' prefix."""
        if self.exclude_genres:
            # Merge exclude_genres into genres with '!' prefix
            self.genres = self.genres or []
            for genre in self.exclude_genres:
                exclusion = f"!{genre}" if not genre.startswith("!") else genre
                if exclusion not in self.genres:
                    self.genres.append(exclusion)
            # Clear deprecated field
            self.exclude_genres = None
        return self


class LibraryProfile(BaseModel):
    """Library profile configuration for organizing media into different libraries"""

    name: str = Field(description="Human-readable profile name")
    library_path: str = Field(
        description="VFS path prefix for this profile (e.g., '/kids', '/anime')"
    )
    enabled: bool = Field(default=True, description="Enable this profile")
    filter_rules: LibraryProfileFilterRules = Field(
        default_factory=lambda: LibraryProfileFilterRules(),
        description="Metadata filter rules for matching items",
    )

    @field_validator("library_path")
    def validate_library_path(cls, v: str):
        """Validate library_path format"""
        if not v:
            raise ValueError("library_path cannot be empty")
        if not v.startswith("/"):
            raise ValueError("library_path must start with '/'")
        if v == "/default":
            raise ValueError(
                "library_path cannot be '/default' (reserved for default path)"
            )
        # Check for valid characters (alphanumeric, dash, underscore, slash)
        import re

        if not re.match(r"^/[a-zA-Z0-9_\-/]+$", v):
            raise ValueError(
                "library_path must contain only alphanumeric characters, dashes, underscores, and slashes"
            )
        return v


class FilesystemModel(Observable):
    mount_path: Path = Field(
        default=Path("/path/to/riven/mount"),
        description="Path where Riven will mount the virtual filesystem",
    )

    library_profiles: dict[str, LibraryProfile] = Field(
        default_factory=lambda: {
            "anime": LibraryProfile(
                name="Anime",
                library_path="/anime",
                enabled=True,
                filter_rules=LibraryProfileFilterRules(is_anime=True),
            ),
            # Example profile (disabled by default) - enable or customize as needed
            # These demonstrate all available filter options
            "example_kids": LibraryProfile(
                name="Kids & Family Content",
                library_path="/kids",
                enabled=False,
                filter_rules=LibraryProfileFilterRules(
                    content_types=["movie", "show"],
                    genres=["animation", "family", "!horror"],
                    # US Movie Ratings: G, PG, PG-13, R, NC-17, NR (Not Rated), Unrated
                    # US TV Ratings: TV-Y, TV-Y7, TV-G, TV-PG, TV-14, TV-MA
                    content_ratings=["G", "PG", "TV-Y", "TV-Y7", "TV-G", "TV-PG"],
                    max_rating=7.5,
                ),
            ),
        },
        description=(
            "Library profiles for organizing media into different libraries based on metadata. "
            "An example profile is provided (disabled by default) - enable them or create your own. "
            "Each profile filters media by metadata (genres, ratings, etc.) and creates VFS paths. "
            "Media appears in all matching profile paths. Use '!' prefix in filter lists to exclude values "
            "(e.g., genres: ['action', '!horror'] = action movies but not horror)."
        ),
    )
    cache_dir: Path = Field(
        default=Path("/dev/shm/riven-cache"),
        description="Directory for caching downloaded chunks",
    )
    cache_max_size_mb: int = Field(
        default=10240, ge=0, description="Maximum cache size in MB (10 GiB default)"
    )
    cache_ttl_seconds: int = Field(
        default=2 * 60 * 60,
        description="Cache time-to-live in seconds (2 hours default)",
    )
    cache_eviction: Literal["LRU", "TTL"] = Field(
        default="LRU", description="Cache eviction policy (LRU or TTL)"
    )
    cache_metrics: bool = Field(
        default=True, description="Enable cache metrics logging"
    )

    # VFS Naming Templates
    movie_dir_template: str = Field(
        default="{title} ({year}) {{tmdb-{tmdb_id}}}",
        description=(
            "Template for movie directory names. "
            "Available variables: title, year, tmdb_id, imdb_id, resolution, codec, hdr, audio, quality, "
            "is_remux, is_proper, is_repack, is_extended, is_directors_cut, container. "
            "Example: '{title} ({year})' or '{title} ({year}) [{resolution}]'"
        ),
    )

    movie_file_template: str = Field(
        default="{title} ({year})",
        description=(
            "Template for movie file names (without extension). "
            "Available variables: title, year, tmdb_id, imdb_id, resolution, codec, hdr, audio, quality, "
            "remux, proper, repack, extended, directors_cut, edition (string flags, empty if false). "
            "Example: '{title} ({year})' or '{title} ({year}) {edition} [{resolution}] {remux}'"
        ),
    )

    show_dir_template: str = Field(
        default="{title} ({year}) {{tvdb-{tvdb_id}}}",
        description=(
            "Template for show directory names. "
            "Available variables: title, year, tvdb_id, imdb_id. "
            "Example: '{title} ({year})' or '{title} ({year}) {{tvdb-{tvdb_id}}}'"
        ),
    )

    season_dir_template: str = Field(
        default="Season {season:02d}",
        description=(
            "Template for season directory names. "
            "Available variables: season (number), show (parent show data with [title], [year], [tvdb_id], [imdb_id]). "
            "Example: 'Season {season:02d}' or 'S{season:02d}' or '{show[title]} - Season {season}'"
        ),
    )

    episode_file_template: str = Field(
        default="{show[title]} - s{season:02d}e{episode:02d}",
        description=(
            "Template for episode file names (without extension). "
            "Available variables: title, season, episode, "
            "show (parent show data with [title], [year], [tvdb_id], [imdb_id]), "
            "resolution, codec, hdr, audio, quality, remux, proper, repack, extended, directors_cut, edition. "
            "Example: '{show[title]} - s{season:02d}e{episode:02d}' or 'S{season:02d}E{episode:02d} - {title}'. "
            "Multi-episode files automatically use range format (e.g., e01-05) based on episode number formatting."
        ),
    )

    @field_validator("library_profiles")
    def validate_library_profiles(cls, v: dict[str, LibraryProfile]):
        """Validate library profile keys and paths"""
        import re

        for key in v.keys():
            # Profile keys must be lowercase alphanumeric with underscores
            if not re.match(r"^[a-z0-9_]+$", key):
                raise ValueError(
                    f"Profile key '{key}' must be lowercase alphanumeric with underscores only"
                )

            if key == "default":
                raise ValueError("Profile key 'default' is reserved")

        # Check for duplicate library_path values among enabled profiles
        # Disabled profiles are allowed to have duplicate paths since they're not active
        enabled_paths = {}

        for key, profile in v.items():
            if profile.enabled:
                # Normalize path for comparison (strip trailing slashes, ensure leading slash)

                normalized_path = profile.library_path.rstrip("/")
                if not normalized_path.startswith("/"):
                    normalized_path = f"/{normalized_path}"

                # Check if this path is already used by another enabled profile
                if normalized_path in enabled_paths:
                    raise ValueError(
                        f"Duplicate library_path '{profile.library_path}' found in profiles "
                        f"'{enabled_paths[normalized_path]}' and '{key}'. "
                        f"Each enabled library profile must have a unique library_path."
                    )

                # Check for reserved paths
                if normalized_path in ["/movies", "/shows"]:
                    raise ValueError(
                        f"library_path '{profile.library_path}' in profile '{key}' is reserved. "
                        f"The paths '/movies' and '/shows' are reserved for base directories."
                    )

                enabled_paths[normalized_path] = key

        return v

    @field_validator(
        "movie_dir_template",
        "movie_file_template",
        "show_dir_template",
        "season_dir_template",
        "episode_file_template",
    )
    def validate_naming_template(cls, v: str) -> str:
        """Validate that naming template string is syntactically valid."""

        from program.utils.safe_formatter import SafeFormatter

        try:
            # Test template with comprehensive dummy data
            formatter = SafeFormatter()
            test_data = {
                "title": "Test Title",
                "year": 2024,
                "season": 1,
                "episode": 1,
                "show": {
                    "title": "Test Show",
                    "year": 2024,
                    "tvdb_id": "12345",
                    "imdb_id": "tt1234567",
                },
                "season_obj": {"number": 1, "title": "Season 1"},
                "tmdb_id": "12345",
                "tvdb_id": "12345",
                "imdb_id": "tt1234567",
                "resolution": "1080p",
                "codec": "h264",
                "hdr": ["HDR10"],
                "audio": "aac",
                "quality": "BluRay",
                "container": "mkv",
                "remux": "REMUX",
                "proper": "PROPER",
                "repack": "REPACK",
                "extended": "Extended",
                "directors_cut": "Director's Cut",
                "edition": "Extended Director's Cut",
            }

            formatter.format(v, **test_data)

            return v
        except Exception as e:
            raise ValueError(f"Invalid naming template syntax: {e}")


# Content Services


class Updatable(Observable):
    update_interval: int = Field(default=80, description="Update interval in seconds")

    @field_validator("update_interval")
    def check_update_interval(cls, v: int):
        if v < (limit := 5):
            raise ValueError(f"update_interval must be at least {limit} seconds")

        return v


# Updaters


class PlexLibraryModel(Observable):
    enabled: bool = Field(default=False, description="Enable Plex library updates")
    token: str = Field(default="", description="Plex authentication token")
    url: EmptyOrUrl = Field(
        default="http://localhost:32400", description="Plex server URL"
    )
    path_mappings: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional: map Riven library paths to the paths Plex uses, e.g. "
            '{"/mnt/debrid/riven": "\\\\server\\riven"} when Plex runs on another machine. '
            "Leave empty when Plex sees the same paths as Riven."
        ),
    )


class JellyfinLibraryModel(Observable):
    enabled: bool = Field(default=False, description="Enable Jellyfin library updates")
    api_key: str = Field(default="", description="Jellyfin API key")
    url: EmptyOrUrl = Field(
        default="http://localhost:8096", description="Jellyfin server URL"
    )


class EmbyLibraryModel(Observable):
    enabled: bool = Field(default=False, description="Enable Emby library updates")
    api_key: str = Field(default="", description="Emby API key")
    url: EmptyOrUrl = Field(
        default="http://localhost:8096", description="Emby server URL"
    )


class UpdatersModel(Observable):
    updater_interval: int = Field(
        default=120, ge=1, description="Interval in seconds between library updates"
    )
    library_path: Path = Field(
        default=Path("/path/to/library/mount"),
        description="Path to which your media library mount point",
    )
    plex: PlexLibraryModel = Field(
        default_factory=PlexLibraryModel,
        description="Plex library configuration",
    )
    jellyfin: JellyfinLibraryModel = Field(
        default_factory=JellyfinLibraryModel,
        description="Jellyfin library configuration",
    )
    emby: EmbyLibraryModel = Field(
        default_factory=EmbyLibraryModel,
        description="Emby library configuration",
    )


# Content Services


class ListrrModel(Updatable):
    enabled: bool = Field(default=False, description="Enable Listrr integration")
    movie_lists: list[str] = Field(
        default_factory=list, description="Listrr movie list IDs"
    )
    show_lists: list[str] = Field(
        default_factory=list, description="Listrr TV show list IDs"
    )
    api_key: str = Field(default="", description="Listrr API key")
    update_interval: int = Field(
        default=86400, ge=1, description="Update interval in seconds (24 hours default)"
    )


class MdblistModel(Updatable):
    enabled: bool = Field(default=False, description="Enable MDBList integration")
    api_key: str = Field(default="", description="MDBList API key")
    lists: list[int | str] = Field(
        default_factory=list[int | str], description="MDBList list IDs to monitor"
    )
    update_interval: int = Field(
        default=86400, ge=1, description="Update interval in seconds (24 hours default)"
    )


class OverseerrIssuesModel(Observable):
    enabled: bool = Field(
        default=False,
        description=(
            "Open an Overseerr/Seerr issue for released movies and episodes that "
            "still have no file, and resolve it once they're available. "
            "Applies after a restart."
        ),
    )
    after_minutes: int = Field(
        default=30,
        ge=5,
        description="Minutes an item must stay missing before an issue is opened",
    )
    release_grace_hours: int = Field(
        default=24,
        ge=0,
        description=(
            "Hours after the release date before a missing item counts "
            "(releases often appear hours after the air date)"
        ),
    )
    interval: int = Field(default=300, ge=60, description="Seconds between checks")
    user_id: int = Field(
        default=0,
        ge=0,
        description=(
            "Overseerr user the issues and comments come from (needs the "
            "issue permissions; 0 = the API key's owner)"
        ),
    )


class OverseerrModel(Updatable):
    enabled: bool = Field(default=False, description="Enable Overseerr integration")
    url: EmptyOrUrl = Field(
        default="http://localhost:5055", description="Overseerr URL"
    )
    api_key: str = Field(default="", description="Overseerr API key")
    use_webhook: bool = Field(
        default=False, description="Use webhook instead of polling"
    )
    update_interval: int = Field(
        default=60, ge=1, description="Update interval in seconds"
    )
    issues: OverseerrIssuesModel = Field(
        default_factory=lambda: OverseerrIssuesModel(),
        description="Report items Riven can't find a file for as Overseerr issues",
    )


class PlexWatchlistModel(Updatable):
    enabled: bool = Field(
        default=False, description="Enable Plex Watchlist integration"
    )
    rss: list[EmptyOrUrl] = Field(
        default_factory=list, description="Plex Watchlist RSS feed URLs"
    )
    update_interval: int = Field(
        default=60, ge=1, description="Update interval in seconds"
    )


class TraktOauthModel(BaseModel):
    oauth_client_id: str = Field(default="", description="Trakt OAuth client ID")
    oauth_client_secret: str = Field(
        default="", description="Trakt OAuth client secret"
    )
    oauth_redirect_uri: str = Field(default="", description="Trakt OAuth redirect URI")
    access_token: str = Field(default="", description="Trakt OAuth access token")
    refresh_token: str = Field(default="", description="Trakt OAuth refresh token")


class TraktModel(Updatable):
    enabled: bool = Field(default=False, description="Enable Trakt integration")
    api_key: str = Field(default="", description="Trakt API key")
    watchlist: list[str] = Field(
        default_factory=list[str],
        description="Trakt usernames for watchlist monitoring",
    )
    user_lists: list[str] = Field(
        default_factory=list[str],
        description="Trakt user list URLs to monitor",
    )
    collection: list[str] = Field(
        default_factory=list[str],
        description="Trakt usernames for collection monitoring",
    )
    fetch_trending: bool = Field(
        default=False, description="Fetch trending content from Trakt"
    )
    trending_count: int = Field(
        default=10, ge=1, description="Number of trending items to fetch"
    )
    fetch_popular: bool = Field(
        default=False, description="Fetch popular content from Trakt"
    )
    popular_count: int = Field(
        default=10, ge=1, description="Number of popular items to fetch"
    )
    fetch_most_watched: bool = Field(
        default=False, description="Fetch most watched content from Trakt"
    )
    most_watched_period: str = Field(
        default="weekly",
        description="Period for most watched (daily, weekly, monthly, yearly)",
    )
    most_watched_count: int = Field(
        default=10, ge=1, description="Number of most watched items to fetch"
    )
    update_interval: int = Field(
        default=86400, ge=1, description="Update interval in seconds (24 hours default)"
    )
    oauth: TraktOauthModel = Field(
        default_factory=lambda: TraktOauthModel(),
        description="Trakt OAuth configuration",
    )
    proxy_url: EmptyOrUrl = Field(
        default="", description="Proxy URL for Trakt API requests"
    )


class ContentModel(Observable):
    overseerr: OverseerrModel = Field(
        default_factory=lambda: OverseerrModel(), description="Overseerr configuration"
    )
    plex_watchlist: PlexWatchlistModel = Field(
        default_factory=lambda: PlexWatchlistModel(),
        description="Plex Watchlist configuration",
    )
    mdblist: MdblistModel = Field(
        default_factory=lambda: MdblistModel(), description="MDBList configuration"
    )
    listrr: ListrrModel = Field(
        default_factory=lambda: ListrrModel(), description="Listrr configuration"
    )
    trakt: TraktModel = Field(
        default_factory=lambda: TraktModel(), description="Trakt configuration"
    )


# Scraper Services


class TorrentioConfig(Observable):
    enabled: bool = Field(default=False, description="Enable Torrentio scraper")
    filter: str = Field(
        default="sort=qualitysize%7Cqualityfilter=480p,scr,cam",
        description="Torrentio filter parameters",
    )
    url: EmptyOrUrl = Field(
        default="http://torrentio.strem.fun", description="Torrentio URL"
    )
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")
    proxy_url: EmptyOrUrl = Field(
        default="", description="Proxy URL for Torrentio requests"
    )


class DebridioConfig(Observable):
    enabled: bool = Field(
        default=False, description="Enable Debridio scraper (TorBox only)"
    )
    api_key: str = Field(default="", description="Debridio API key")
    url: EmptyOrUrl = Field(
        default="https://addon.debridio.com", description="Debridio addon URL"
    )
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")
    proxy_url: EmptyOrUrl = Field(
        default="", description="Proxy URL for Debridio requests"
    )


class CometConfig(Observable):
    enabled: bool = Field(default=False, description="Enable Comet scraper")
    url: EmptyOrUrl = Field(default="http://localhost:8000", description="Comet URL")
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")


class ZileanConfig(Observable):
    enabled: bool = Field(default=False, description="Enable Zilean scraper")
    url: EmptyOrUrl = Field(default="http://localhost:8181", description="Zilean URL")
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")


class MediafusionConfig(Observable):
    enabled: bool = Field(default=False, description="Enable Mediafusion scraper")
    url: EmptyOrUrl = Field(
        default="http://localhost:8000", description="Mediafusion URL"
    )
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")


class OrionoidConfigParametersDict(Observable):
    video3d: bool = Field(default=False, description="Include 3D video results")
    videoquality: str = Field(default="sd_hd8k", description="Video quality filter")
    limitcount: int = Field(default=5, ge=1, description="Maximum number of results")


class OrionoidConfig(Observable):
    enabled: bool = Field(default=False, description="Enable Orionoid scraper")
    api_key: str = Field(default="", description="Orionoid API key")
    cached_results_only: bool = Field(
        default=False, description="Only return cached/downloadable results"
    )
    parameters: OrionoidConfigParametersDict = Field(
        default_factory=lambda: OrionoidConfigParametersDict(),
        description="Additional Orionoid parameters",
    )
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")


class JackettConfig(Observable):
    enabled: bool = Field(default=False, description="Enable Jackett scraper")
    url: EmptyOrUrl = Field(default="http://localhost:9117", description="Jackett URL")
    api_key: str = Field(default="", description="Jackett API key")
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    infohash_fetch_timeout: int = Field(
        default=30,
        ge=1,
        description="Timeout in seconds for parallel infohash fetching from URLs",
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")


class ProwlarrConfig(Observable):
    enabled: bool = Field(default=False, description="Enable Prowlarr scraper")
    url: EmptyOrUrl = Field(default="http://localhost:9696", description="Prowlarr URL")
    api_key: str = Field(default="", description="Prowlarr API key")
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    infohash_fetch_timeout: int = Field(
        default=30,
        ge=1,
        description="Timeout in seconds for parallel infohash fetching from URLs",
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")
    limiter_seconds: int = Field(
        default=60, ge=1, description="Rate limiter cooldown in seconds"
    )


class RarbgConfig(Observable):
    enabled: bool = Field(default=False, description="Enable RARBG scraper")
    url: EmptyOrUrl = Field(default="https://therarbg.to", description="RARBG URL")
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")


class AIOStreamsConfig(Observable):
    enabled: bool = Field(default=False, description="Enable AIOStreams scraper")
    url: EmptyOrUrl = Field(
        default="http://localhost:8000", description="AIOStreams instance URL"
    )
    timeout: int = Field(default=30, ge=1, description="Request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, description="Number of retries for failed requests"
    )
    ratelimit: bool = Field(default=True, description="Enable rate limiting")
    proxy_url: EmptyOrUrl = Field(
        default="", description="Proxy URL for AIOStreams requests"
    )
    uuid: str = Field(default="", description="User UUID for AIOStreams authentication")
    password: str = Field(
        default="", description="User password for AIOStreams authentication"
    )


ANIME_AUDIO_TYPES = ("dual", "japanese", "dubbed")


class BlacklistModel(Observable):
    expiry_enabled: bool = Field(
        default=True,
        description=(
            "Let blacklist entries for temporary problems expire, so the torrent "
            "goes back into the item's pool of sources. Wrong files, manual "
            "blacklists and entries from before this setting never expire."
        ),
    )
    cdn_host_down_hours: float = Field(
        default=24,
        ge=0,
        description="TorBox only: hours a torrent stays blacklisted after its CDN server was pulled (0 = never expires)",
    )
    slow_source_hours: float = Field(
        default=24,
        ge=0,
        description="Hours a torrent stays blacklisted after it was too slow to play (0 = never expires)",
    )
    not_cached_hours: float = Field(
        default=72,
        ge=0,
        description="Hours a torrent stays blacklisted after it wasn't cached on the debrid service (0 = never expires)",
    )
    torrent_removed_hours: float = Field(
        default=24,
        ge=0,
        description="Hours a torrent stays blacklisted after it or its file disappeared from the debrid account (0 = never expires)",
    )
    download_error_hours: float = Field(
        default=72,
        ge=0,
        description="Hours a torrent stays blacklisted after any other download failure (0 = never expires)",
    )
    max_strikes: int = Field(
        default=4,
        ge=0,
        description=(
            "Each time the same torrent is blacklisted again for the same item, "
            "its time doubles; once that has happened more than this many times "
            "it stays blacklisted for good (0 = never permanent)"
        ),
    )
    check_interval: int = Field(
        default=900,
        ge=60,
        description="Seconds between checks for expired blacklist entries",
    )


class ScraperModel(Observable):
    after_2: float = Field(
        default=2, description="Hours to wait after 2 failed scrapes"
    )
    after_5: float = Field(
        default=6, description="Hours to wait after 5 failed scrapes"
    )
    after_10: float = Field(
        default=24, description="Hours to wait after 10 failed scrapes"
    )
    enable_aliases: bool = Field(
        default=True, description="Enable title aliases for better matching"
    )
    bucket_limit: int = Field(
        default=5, ge=0, le=20, description="Maximum results per quality bucket"
    )
    max_failed_attempts: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Maximum failed scrape attempts before giving up",
    )
    dubbed_anime_only: bool = Field(
        default=False, description="Only scrape dubbed anime content"
    )
    anime_audio_order_enabled: bool = Field(
        default=False,
        description="Anime only: rank releases by the audio order below (within each resolution)",
    )
    anime_audio_order: list[Literal["dual", "japanese", "dubbed"]] = Field(
        default_factory=lambda: list(ANIME_AUDIO_TYPES),
        min_length=3,
        max_length=3,
        description=(
            "Preferred anime audio, best first; reorder with the arrows. "
            "dual = Japanese + English, japanese = Japanese audio (untagged releases count), "
            "dubbed = dubbed only."
        ),
        json_schema_extra={
            "items": {
                "type": "string",
                "readOnly": True,
                "x-ui-options": {"hideTitle": True},
            },
            "x-ui-options": {"addable": False, "removable": False},
        },
    )

    @model_validator(mode="before")
    @classmethod
    def _complete_anime_audio_order(cls, data: Any) -> Any:
        # Older settings used an empty list for "off" and allowed partial lists.
        if not isinstance(data, dict) or "anime_audio_order" not in data:
            return data
        order = data["anime_audio_order"]
        if not isinstance(order, list):
            return data
        data = dict(data)
        data.setdefault("anime_audio_order_enabled", bool(order))
        if len(set(order)) == len(order):
            data["anime_audio_order"] = order + [
                t for t in ANIME_AUDIO_TYPES if t not in order
            ]
        return data

    @field_validator("anime_audio_order")
    @classmethod
    def _unique_anime_audio_order(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("anime_audio_order must not repeat an audio type")
        return value

    torrentio: TorrentioConfig = Field(
        default_factory=lambda: TorrentioConfig(), description="Torrentio configuration"
    )
    debridio: DebridioConfig = Field(
        default_factory=lambda: DebridioConfig(), description="Debridio configuration"
    )
    jackett: JackettConfig = Field(
        default_factory=lambda: JackettConfig(), description="Jackett configuration"
    )
    prowlarr: ProwlarrConfig = Field(
        default_factory=lambda: ProwlarrConfig(), description="Prowlarr configuration"
    )
    orionoid: OrionoidConfig = Field(
        default_factory=lambda: OrionoidConfig(), description="Orionoid configuration"
    )
    mediafusion: MediafusionConfig = Field(
        default_factory=lambda: MediafusionConfig(),
        description="Mediafusion configuration",
    )
    zilean: ZileanConfig = Field(
        default_factory=lambda: ZileanConfig(), description="Zilean configuration"
    )
    comet: CometConfig = Field(
        default_factory=lambda: CometConfig(), description="Comet configuration"
    )
    rarbg: RarbgConfig = Field(
        default_factory=lambda: RarbgConfig(), description="RARBG configuration"
    )
    aiostreams: AIOStreamsConfig = Field(
        default_factory=lambda: AIOStreamsConfig(),
        description="AIOStreams configuration",
    )
    blacklist: BlacklistModel = Field(
        default_factory=lambda: BlacklistModel(),
        description="When blacklisted torrents go back into an item's pool of sources",
    )


# Version Ranking Model (set application defaults here!)


class RTNSettingsModel(SettingsModel, Observable):
    @field_validator("languages")
    @classmethod
    def _check_language_codes(cls, value: Any) -> Any:
        """Lower-case and de-duplicate language codes; reject codes PTT never detects."""
        from PTT.parse import LANGUAGES_TRANSLATION_TABLE

        unknown: set[str] = set()
        for field in ("required", "allowed", "exclude", "preferred"):
            if (
                field not in type(value).model_fields
            ):  # "allowed" is newer than RTN 1.9.3
                continue
            codes = [c.strip().lower() for c in getattr(value, field, None) or []]
            unknown.update(c for c in codes if c not in LANGUAGES_TRANSLATION_TABLE)
            setattr(value, field, list(dict.fromkeys(codes)))
        if unknown:
            raise ValueError(
                f"Unknown language code(s): {', '.join(sorted(unknown))}. "
                f"Valid codes: {', '.join(sorted(LANGUAGES_TRANSLATION_TABLE))}"
            )
        return value


# Application Settings


class IndexerModel(Observable):
    schedule_offset_minutes: int = Field(
        default=30,
        ge=0,
        description="Offset in minutes after aired_at time to schedule scraping for episodes and movies (30 minutes default)",
    )


class DatabaseModel(Observable):
    host: PostgresDsn = Field(
        default_factory=lambda: PostgresDsn(
            "postgresql+psycopg2://postgres:postgres@localhost/riven"
        ),
        description="Database connection string",
    )


class NotificationsModel(Observable):
    enabled: bool = Field(default=False, description="Enable notifications")
    on_item_type: list[str] = Field(
        default_factory=lambda: ["movie", "show", "season", "episode"],
        description="Item types to send notifications for",
        json_schema_extra={
            "uniqueItems": True,
            "items": {"type": "string", "enum": ["movie", "show", "season", "episode"]},
        },
    )
    service_urls: list[str] = Field(
        default_factory=list,
        description="Notification service URLs (e.g., Discord webhooks)",
    )


class SubtitleProviderConfig(Observable):
    enabled: bool = Field(default=False, description="Enable this subtitle provider")


class SubtitleProvidersDict(Observable):
    opensubtitles: SubtitleProviderConfig = Field(
        default_factory=lambda: SubtitleProviderConfig(),
        description="OpenSubtitles provider configuration",
    )


class SubtitleConfig(Observable):
    enabled: bool = Field(default=False, description="Enable subtitle downloading")
    languages: list[str] = Field(
        default_factory=lambda: ["eng"],
        description="Subtitle languages to download (ISO 639-2 codes)",
    )
    providers: SubtitleProvidersDict = Field(
        default_factory=lambda: SubtitleProvidersDict(),
        description="Subtitle provider configurations",
    )


class PostProcessing(Observable):
    subtitle: SubtitleConfig = Field(
        default_factory=lambda: SubtitleConfig(),
        description="Subtitle post-processing configuration",
    )


class LoggingModel(Observable):
    enabled: bool = Field(default=True, description="Enable file logging")
    clean_interval: int = Field(
        default=60 * 60, description="Log cleanup interval in seconds (1 hour default)"
    )
    retention_hours: int = Field(
        default=24, description="Log retention period in hours"
    )
    rotation_mb: int = Field(default=10, description="Log file rotation size in MB")
    compression: Literal["zip", "gz", "bz2", "xz", "disabled"] = Field(
        default="disabled",
        description="Log compression format (empty for no compression)",
    )

    @field_validator("compression", mode="before")
    def check_compression(cls, v: str | None):
        if v == "" or not v:
            return "disabled"

        return v


class SlowStreamModel(Observable):
    enabled: bool = Field(
        default=False,
        description=(
            "Replace a file whose source is too slow to play. When a file Plex is "
            "playing keeps timing out or downloads slower than it plays, Riven "
            "first tries a fresh download link; if it is still slow, it blacklists "
            "that torrent for the item, stops the Plex playback with the message "
            "below and finds another copy (same path in Plex). Several files from "
            "different torrents slow at once is treated as a connection or debrid "
            "problem: nothing is replaced, the notify URLs are told."
        ),
    )
    min_speed_ratio: float = Field(
        default=1.2,
        ge=1.0,
        description=(
            "A file is slow when it downloads at less than this many times its "
            "bitrate (file size / duration in Plex)"
        ),
    )
    slow_seconds: int = Field(
        default=60,
        ge=15,
        description="How long a file must download that slowly before it counts",
    )
    stall_count: int = Field(
        default=2,
        ge=1,
        description=(
            "A file is also slow after this many reads that timed out "
            "(chunk_wait_timeout_seconds) within stall_window_seconds"
        ),
    )
    stall_window_seconds: int = Field(
        default=300, ge=30, description="Window for stall_count, in seconds"
    )
    cooldown_hours: int = Field(
        default=6,
        ge=1,
        description="Replace the same movie or episode at most once in this many hours",
    )
    stop_playback_message: str = Field(
        default=(
            "This video's source is too slow, so it is being switched to a faster "
            "copy. Press play again in a minute or two to continue where you left off."
        ),
        description="Message Plex shows when playback is stopped for a replace",
    )
    notify_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Apprise URLs (e.g. a Discord webhook) told about each replace, and "
            "when several files are slow at once"
        ),
    )


class StreamModel(Observable):
    chunk_size_mb: int = Field(
        default=1,
        ge=1,
        description="Chunk size in MB for streaming downloads (1 MB default). Note: Smaller chunks are generally more efficient, as the entire chunk must be downloaded before it can be read.",
    )
    connect_timeout_seconds: int = Field(
        default=10,
        ge=1,
        description="Timeout in seconds for establishing a connection to the streaming service (10 seconds default)",
    )
    chunk_wait_timeout_seconds: int = Field(
        default=10,
        ge=1,
        description="Timeout in seconds for reading a chunk during streaming (10 seconds default)",
    )
    activity_timeout_seconds: int = Field(
        default=60,
        ge=1,
        description="Timeout in seconds before a stream is considered inactive during resource cleanup (60 seconds default)",
    )
    slow_replace: SlowStreamModel = Field(
        default_factory=lambda: SlowStreamModel(),
        description="Replace sources too slow to play",
    )


class AppModel(Observable):
    version: str = Field(default_factory=get_version, description="Application version")
    api_key: str = Field(default="", description="API key for Riven API access")
    log_level: Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = (
        Field(default="INFO", description="Logging level")
    )
    enable_network_tracing: bool = Field(
        default=False,
        description="Enable detailed network request/response logging",
    )
    enable_stream_tracing: bool = Field(
        default=False,
        description="Enable detailed stream request/response logging",
    )
    retry_interval: int = Field(
        default=60 * 60 * 24,
        ge=0,
        description="Interval in seconds to retry failed library items (24 hours default, 0 to disable)",
    )
    tracemalloc: bool = Field(
        default=False, description="Enable Python memory tracking (debug)"
    )
    filesystem: FilesystemModel = Field(
        default_factory=lambda: FilesystemModel(),
        description="Filesystem configuration",
    )
    updaters: UpdatersModel = Field(
        default_factory=lambda: UpdatersModel(),
        description="Library updaters configuration",
    )
    downloaders: DownloadersModel = Field(
        default_factory=lambda: DownloadersModel(),
        description="Downloader services configuration",
    )
    content: ContentModel = Field(
        default_factory=lambda: ContentModel(),
        description="Content services configuration",
    )
    scraping: ScraperModel = Field(
        default_factory=lambda: ScraperModel(), description="Scraper configuration"
    )
    ranking: RTNSettingsModel = Field(
        default_factory=lambda: RTNSettingsModel(),
        description="Result ranking configuration",
    )
    indexer: IndexerModel = Field(
        default_factory=lambda: IndexerModel(), description="Indexer configuration"
    )
    database: DatabaseModel = Field(
        default_factory=lambda: DatabaseModel(), description="Database configuration"
    )
    notifications: NotificationsModel = Field(
        default_factory=lambda: NotificationsModel(),
        description="Notifications configuration",
    )
    post_processing: PostProcessing = Field(
        default_factory=lambda: PostProcessing(),
        description="Post-processing configuration",
    )
    logging: LoggingModel = Field(
        default_factory=lambda: LoggingModel(), description="Logging configuration"
    )
    stream: StreamModel = Field(
        default_factory=lambda: StreamModel(), description="Stream configuration"
    )

    @field_validator("log_level", mode="before")
    def check_debug(cls, v: str | bool):
        if v == True:
            return "DEBUG"
        elif v == False:
            return "INFO"

        return v.upper()

    def __init__(self, **data: Any):
        current_version = get_version()
        existing_version = data.get("version", current_version)
        super().__init__(**data)
        if existing_version < current_version:
            self.version = current_version

        if self.api_key == "":
            self.api_key = generate_api_key()
