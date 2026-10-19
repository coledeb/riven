from __future__ import annotations

import contextlib
import ipaddress
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger
from requests import exceptions
from sqlalchemy import select

from program.db.db import db_session
from program.media.item import ProcessedItemType
from program.media.media_entry import MediaEntry
from program.services.downloaders.models import (
    DebridFile,
    InvalidDebridFileException,
    TorrentContainer,
    TorrentFile,
    TorrentInfo,
    UnrestrictedLink,
    UserInfo,
)
from program.services.streaming.exceptions.debrid_service_exception import (
    DebridServiceLinkUnavailable,
)
from program.settings import settings_manager
from program.utils import get_version
from program.utils.request import (
    CircuitBreakerOpen,
    SmartResponse,
    SmartSession,
    TokenBucket,
)

from .shared import DownloaderBase, premium_days_left

# TorBox documents 300 requests/min per API key. Stay under it so other
# clients sharing the key (e.g. a mount) keep some headroom.
API_RATE_PER_SECOND = 240 / 60
API_BURST = 10

# requestdl has its own, much smaller budget (about 100 calls per rolling five
# minutes, measured); a 429 there locks link creation for ~5 minutes.
LINK_RATE_PER_SECOND = 15 / 60
LINK_BURST = 10

# TorBox answers 429s with Retry-After: 300. Used when the header is missing.
DEFAULT_PAUSE_SECONDS = 300
MAX_PAUSE_SECONDS = 3600

LIST_PAGE_SIZE = 1000


class TorBoxError(Exception):
    """Base exception for TorBox related errors."""


class TorBoxNotFound(TorBoxError):
    """The torrent is not (or no longer) on the TorBox account."""


class TorBoxRateLimited(CircuitBreakerOpen):
    """
    TorBox refused requests with a 429 and they are paused.

    Subclasses CircuitBreakerOpen so the downloader puts the service in
    cooldown and retries later instead of blacklisting the stream.
    """

    def __init__(self, kind: str, seconds_left: float) -> None:
        super().__init__(f"TorBox ({kind} requests paused {int(seconds_left)}s)")
        self.kind = kind


class TorBoxAPI:
    """Minimal TorBox API client."""

    BASE_URL = "https://api.torbox.app/v1/api"

    def __init__(self, api_key: str, proxy_url: str | None = None) -> None:
        self.api_key = api_key

        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        try:
            version = get_version()
        except Exception:
            version = "Unknown"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"Riven/{version} TorBox/1.0",
        }

        # retries=0: SmartSession would otherwise sleep out TorBox's 300s
        # Retry-After inline. 429s are handled by TorBoxDownloader instead.
        self.session = SmartSession(
            base_url=self.BASE_URL,
            rate_limits={
                "api.torbox.app": {"rate": API_RATE_PER_SECOND, "capacity": API_BURST},
            },
            proxies=proxies,
            retries=0,
        )
        self.session.headers.update(headers)

        # Separate session for requestdl: TorBox answers links for vanished
        # files with HTTP 500, which must not trip the API circuit breaker.
        # Paced by TorBoxDownloader's own link bucket.
        self.link_session = SmartSession(
            base_url=self.BASE_URL,
            proxies=proxies,
            retries=0,
        )
        self.link_session.headers.update(headers)


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _host_offline(url: str) -> bool:
    """True if the URL's host resolves only to unspecified addresses (0.0.0.0 / ::)."""

    host = urlparse(url).hostname

    if not host:
        return False

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # A DNS hiccup isn't proof the server is gone
        return False

    return bool(infos) and all(
        ipaddress.ip_address(info[4][0]).is_unspecified for info in infos
    )


def _error_text(response: SmartResponse, body: dict[str, Any]) -> str:
    return str(
        body.get("detail") or body.get("error") or f"HTTP {response.status_code}"
    )


class TorBoxDownloader(DownloaderBase):
    """
    TorBox downloader.

    Notes:
    - Only cached torrents are added (checkcached first, then createtorrent
      with add_only_if_cached), so nothing is ever downloaded from peers.
    - TorBox keeps one entry per hash per account: adding a hash that is
      already there returns the existing torrent. Such torrents may belong to
      other tools sharing the account, so by default Riven only deletes
      torrents that it created itself during this run
      (``only_manage_own_torrents``). Torrents that Riven's library still
      links to are never deleted.
    - File links are stored without the API key and resolved through
      requestdl only when the VFS needs them.
    """

    def __init__(self) -> None:
        self.key = "torbox"
        self.settings = settings_manager.settings.downloaders.torbox
        self.api: TorBoxAPI | None = None

        self._lock = threading.Lock()
        self._created = set[str]()
        self._paused_until = dict[str, float]()
        self._link_bucket = TokenBucket(
            rate=LINK_RATE_PER_SECOND,
            capacity=LINK_BURST,
            name="torbox-requestdl",
        )

        self.initialized = self.validate()

    def validate(self) -> bool:
        """
        Validate settings and current premium status.

        Returns:
            True if ready, else False.
        """

        if not self._validate_settings():
            return False

        self.api = TorBoxAPI(
            api_key=self.settings.api_key,
            proxy_url=self.PROXY_URL or None,
        )

        return self._validate_premium()

    def _validate_settings(self) -> bool:
        if not self.settings.enabled:
            return False

        if not self.settings.api_key:
            logger.warning("TorBox API key is not set")
            return False

        return True

    def _validate_premium(self) -> bool:
        user_info = self.get_user_info()

        if not user_info:
            logger.error("Failed to get TorBox user info")
            return False

        if user_info.premium_status != "premium":
            logger.error("TorBox paid plan required")
            return False

        if user_info.premium_expires_at:
            logger.info(premium_days_left(user_info.premium_expires_at))

        return True

    # --- rate limit handling ---

    def _pause(self, kind: str, response: SmartResponse) -> None:
        """Record a 429 for `kind` requests and raise TorBoxRateLimited."""

        wait = float(DEFAULT_PAUSE_SECONDS)
        retry_after = response.headers.get("Retry-After")

        if retry_after:
            with contextlib.suppress(ValueError):
                wait = min(max(float(retry_after), 1.0), MAX_PAUSE_SECONDS)

        until = time.monotonic() + wait

        with self._lock:
            self._paused_until[kind] = max(self._paused_until.get(kind, 0.0), until)

        logger.warning(
            f"TorBox rate limit reached; pausing {kind} requests for {int(wait)}s"
        )

        raise TorBoxRateLimited(kind, wait)

    def _check_pause(self, *kinds: str) -> None:
        now = time.monotonic()

        with self._lock:
            for kind in kinds:
                until = self._paused_until.get(kind, 0.0)

                if until > now:
                    raise TorBoxRateLimited(kind, until - now)

    def _request(
        self,
        method: str,
        endpoint: str,
        kind: str = "api",
        **kwargs: Any,
    ) -> tuple[SmartResponse, dict[str, Any]]:
        """
        Send an API request and return (response, decoded JSON body).

        Raises:
            TorBoxRateLimited: If TorBox answered 429 or `kind` requests are paused.
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
        """

        assert self.api

        self._check_pause("api", kind)

        try:
            response = self.api.session.request(method, endpoint, **kwargs)
        except (exceptions.ConnectionError, exceptions.Timeout):
            time.sleep(1)
            response = self.api.session.request(method, endpoint, **kwargs)

        if response.status_code == 429:
            self._pause(kind, response)

        try:
            body = response.json()
        except ValueError:
            body = {}

        return response, body if isinstance(body, dict) else {}

    # --- account listing ---

    def _find_torrent(self, infohash: str) -> dict[str, Any] | None:
        """Return the account's torrent for `infohash`, read fresh from TorBox."""

        return next(
            (
                t
                for t in self._iter_account()
                if str(t.get("hash", "")).lower() == infohash
            ),
            None,
        )

    def list_torrents(self) -> list[dict[str, Any]]:
        """Every torrent on the account, read fresh from TorBox."""

        return list(self._iter_account())

    def created_ids(self) -> set[str]:
        """Torrent ids Riven added during this run."""

        with self._lock:
            return set(self._created)

    def _iter_account(self):
        """Yield the account's torrents page by page, read fresh from TorBox."""

        offset = 0
        seen = set[int]()

        while True:
            # bypass_cache: the default list lags up to ~5 minutes behind.
            # mylist returns at most 1000 entries and no total, so page until short.
            response, body = self._request(
                "GET",
                "torrents/mylist",
                params={
                    "bypass_cache": "true",
                    "offset": offset,
                    "limit": LIST_PAGE_SIZE,
                },
            )

            if not response.ok:
                raise TorBoxError(
                    f"Failed to list torrents: {_error_text(response, body)}"
                )

            page = body.get("data") or []
            new_ids = {t.get("id") for t in page} - seen

            yield from (t for t in page if t.get("id") in new_ids)

            if len(page) < LIST_PAGE_SIZE or not new_ids:
                return

            seen |= new_ids
            offset += len(page)

    def _is_cached(self, infohash: str) -> bool:
        """
        Ask TorBox whether it has the content cached.

        checkcached costs nothing against the createtorrent limit, so it
        filters out uncached hashes before an add is attempted. If the check
        itself fails, return True and let add_only_if_cached decide.
        """

        try:
            response, body = self._request(
                "GET",
                "torrents/checkcached",
                params={"hash": infohash, "format": "object", "list_files": "false"},
            )
        except CircuitBreakerOpen:
            raise
        except Exception as e:
            logger.debug(f"TorBox cache check failed for {infohash}: {e}")
            return True

        if not response.ok or not body.get("success"):
            logger.debug(
                f"TorBox cache check failed for {infohash}: {_error_text(response, body)}"
            )
            return True

        data = body.get("data") or {}

        return any(str(h).lower() == infohash for h in data)

    # --- DownloaderBase ---

    def get_instant_availability(
        self,
        infohash: str,
        item_type: ProcessedItemType,
        **_: Any,
    ) -> TorrentContainer | None:
        """
        Check whether `infohash` is cached and, if so, add it and return its files.

        Riven's download step reuses the torrent id from the container, so a
        cached torrent is added here, like the other providers do.
        """

        infohash = infohash.lower()
        torrent_id: str | None = None

        try:
            if not self._is_cached(infohash):
                logger.debug(f"Torrent {infohash} is not cached on TorBox")
                return None

            torrent_id = self.add_torrent(infohash)
            container, reason, info = self._process_torrent(
                torrent_id, infohash, item_type
            )

            if container is None:
                logger.debug(f"Availability check failed [{infohash}]: {reason}")
                self._discard(torrent_id)
                return None

            container.torrent_id = torrent_id
            container.torrent_info = info

            return container
        except CircuitBreakerOpen:
            self._discard(torrent_id)
            raise
        except InvalidDebridFileException as e:
            logger.debug(
                f"Availability check failed [{infohash}]: Invalid debrid file(s) - {e}"
            )
            self._discard(torrent_id)
            return None
        except Exception as e:
            logger.debug(f"Availability check failed [{infohash}]: {e}")
            self._discard(torrent_id)
            return None

    def _process_torrent(
        self,
        torrent_id: str,
        infohash: str,
        item_type: ProcessedItemType,
    ) -> tuple[TorrentContainer | None, str | None, TorrentInfo | None]:
        """
        Process a single torrent and return (container, reason, info).

        Returns:
            (TorrentContainer or None, human-readable reason string if None, TorrentInfo or None)
        """

        info = self.get_torrent_info(torrent_id)

        if not info.files:
            return None, "no files present in the torrent", None

        if info.status != "downloaded":
            return None, f"Not instantly available (status={info.status})", None

        files = list[DebridFile]()

        for file_id, file in info.files.items():
            try:
                df = DebridFile.create(
                    path=file.path,
                    filename=file.filename,
                    filesize_bytes=file.bytes,
                    filetype=item_type,
                    file_id=file_id,
                )
                df.download_url = file.download_url
                files.append(df)
            except InvalidDebridFileException as e:
                logger.debug(f"{infohash}: {e}")

        if not files:
            return None, "no valid files after validation", None

        return TorrentContainer(infohash=infohash, files=files), None, info

    def add_torrent(self, infohash: str) -> str:
        """
        Add a cached torrent by infohash, or reuse the account's existing one.

        Returns:
            TorBox torrent id.

        Raises:
            TorBoxRateLimited: If TorBox's add limit was hit.
            TorBoxError: If TorBox refused the add.
        """

        infohash = infohash.lower()
        existing = self._find_torrent(infohash)

        if existing:
            torrent_id = str(existing["id"])
            logger.debug(
                f"Torrent {infohash} is already on TorBox as {torrent_id}; reusing it"
            )
            return torrent_id

        response, body = self._request(
            "POST",
            "torrents/createtorrent",
            kind="add",
            data={
                "magnet": f"magnet:?xt=urn:btih:{infohash}",
                "add_only_if_cached": "true",
            },
        )

        if not response.ok or not body.get("success"):
            raise TorBoxError(
                f"Failed to add torrent {infohash}: {_error_text(response, body)}"
            )

        # createtorrent usually answers with an object, occasionally with a list.
        data = body.get("data")
        entries = (
            data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        )
        torrent_id = None

        for entry in entries:
            if len(entries) == 1 or str(entry.get("hash", "")).lower() == infohash:
                torrent_id = entry.get("torrent_id") or entry.get("id")
                break

        if not torrent_id:
            raise TorBoxError(f"No torrent ID returned by TorBox for {infohash}")

        torrent_id = str(torrent_id)

        with self._lock:
            self._created.add(torrent_id)

        return torrent_id

    def select_files(self, torrent_id: int | str, file_ids: list[int]) -> None:
        """TorBox makes every file of a torrent available; nothing to select."""

    def get_torrent_info(self, torrent_id: int | str) -> TorrentInfo:
        """
        Get information about a specific torrent using its ID.

        Raises:
            TorBoxNotFound: If the torrent is not on the account.
            TorBoxError: If the API returns a failing status.
        """

        response, body = self._request(
            "GET",
            "torrents/mylist",
            params={"id": torrent_id, "bypass_cache": "true"},
        )

        if response.status_code == 404:
            raise TorBoxNotFound(f"Torrent {torrent_id} not found on TorBox")

        if not response.ok:
            raise TorBoxError(
                f"Failed to get torrent {torrent_id}: {_error_text(response, body)}"
            )

        # mylist?id= usually answers with an object, occasionally with a one-element list.
        data = body.get("data")

        if isinstance(data, list):
            data = data[0] if data else None

        if not data:
            raise TorBoxNotFound(f"Torrent {torrent_id} not found on TorBox")

        files = dict[int, TorrentFile]()

        for file in data.get("files") or []:
            file_id = int(file["id"])
            files[file_id] = TorrentFile(
                id=file_id,
                path=file.get("name") or file.get("short_name") or "",
                bytes=int(file.get("size") or 0),
                selected=1,
                download_url=self._file_link(data["id"], file_id),
            )

        status = (
            "downloaded"
            if data.get("download_finished")
            else data.get("download_state")
        )

        return TorrentInfo(
            id=str(data["id"]),
            name=data.get("name") or "",
            status=status,
            infohash=data.get("hash"),
            progress=float(data.get("progress") or 0) * 100,
            bytes=data.get("size"),
            created_at=_parse_time(data.get("created_at")),
            expires_at=_parse_time(data.get("expires_at")),
            files=files,
            links=[f.download_url for f in files.values()],
        )

    def delete_torrent(self, torrent_id: int | str) -> None:
        """
        Delete a torrent on TorBox.

        With ``only_manage_own_torrents`` on (the default), torrents that were
        already on the account are left alone: other tools sharing the account
        may still be using them. With it off, those are deleted too. A torrent
        Riven's library still links to is never deleted.

        Raises:
            TorBoxError: If the API returns a failing status.
        """

        torrent_id = str(torrent_id)

        with self._lock:
            created = torrent_id in self._created

        if not created and self.settings.only_manage_own_torrents:
            logger.debug(
                f"Leaving TorBox torrent {torrent_id} in place: Riven did not create it"
            )
            return

        # Also for torrents Riven created: a season pack is tried by several
        # episodes at once, and one episode failing on it must not delete the
        # pack another episode has just linked to.
        if self._torrent_in_use(torrent_id):
            logger.debug(
                f"Leaving TorBox torrent {torrent_id} in place: Riven's library still uses it"
            )
            return

        response, body = self._request(
            "POST",
            "torrents/controltorrent",
            json={"torrent_id": int(torrent_id), "operation": "delete"},
        )

        if not response.ok and response.status_code != 404:
            raise TorBoxError(
                f"Failed to delete torrent {torrent_id}: {_error_text(response, body)}"
            )

        with self._lock:
            self._created.discard(torrent_id)

    def _torrent_in_use(self, torrent_id: str) -> bool:
        """
        Whether any stored Riven file link points at this torrent.

        A torrent re-added by hash keeps its id, so a pack can serve many items.
        If the database can't be checked, the torrent counts as in use.
        """

        try:
            link = f"{TorBoxAPI.BASE_URL}/torrents/requestdl?torrent_id={torrent_id}&"
            with db_session() as session:
                found = session.execute(
                    select(MediaEntry.id)
                    .where(MediaEntry.download_url.startswith(link, autoescape=True))
                    .limit(1)
                ).first()
            return found is not None
        except Exception as e:
            logger.warning(
                f"Could not check whether TorBox torrent {torrent_id} is in use; keeping it: {e}"
            )
            return True

    def _discard(self, torrent_id: str | None) -> None:
        """Best-effort removal of a torrent added during a failed check."""

        if not torrent_id:
            return

        try:
            self.delete_torrent(torrent_id)
        except Exception as e:
            logger.debug(f"Failed to delete TorBox torrent {torrent_id}: {e}")

    # --- links ---

    def _file_link(self, torrent_id: int | str, file_id: int) -> str:
        """Provider-restricted link for a file; carries no credentials."""

        return f"{TorBoxAPI.BASE_URL}/torrents/requestdl?torrent_id={torrent_id}&file_id={file_id}"

    @staticmethod
    def _parse_file_link(link: str) -> tuple[str, str] | None:
        query = parse_qs(urlparse(link).query)
        torrent_id = (query.get("torrent_id") or [None])[0]
        file_id = (query.get("file_id") or [None])[0]

        if not torrent_id or file_id is None:
            return None

        return torrent_id, file_id

    def _file_gone(self, torrent_id: str, file_id: str) -> bool:
        """True only if TorBox confirms the torrent or file is no longer on the account."""

        try:
            info = self.get_torrent_info(torrent_id)
        except TorBoxNotFound:
            return True
        except Exception:
            return False

        return int(file_id) not in info.files

    def unrestrict_link(self, link: str) -> UnrestrictedLink | None:
        """
        Resolve a stored file link to a direct CDN URL through requestdl.

        Raises:
            DebridServiceLinkUnavailable: If the torrent or file is gone from
                the account, so Riven can pick another stream.
        """

        assert self.api

        ids = self._parse_file_link(link)

        if not ids:
            logger.warning(f"Not a TorBox file link: {link}")
            return None

        torrent_id, file_id = ids

        try:
            self._check_pause("api", "link")
        except TorBoxRateLimited as e:
            logger.debug(f"Not resolving TorBox link for torrent {torrent_id}: {e}")
            return None

        self._link_bucket.wait()

        try:
            response = self.api.link_session.get(
                "torrents/requestdl",
                params={
                    "token": self.api.api_key,
                    "torrent_id": torrent_id,
                    "file_id": file_id,
                    "redirect": "false",
                },
                timeout=15,
            )
        except Exception as e:
            # The request URL carries the API key; log only the error type.
            logger.debug(
                f"TorBox link request failed for torrent {torrent_id} file {file_id}: {type(e).__name__}"
            )
            return None

        if response.status_code == 429:
            with contextlib.suppress(TorBoxRateLimited):
                self._pause("link", response)

            return None

        try:
            body = response.json()
        except ValueError:
            body = {}

        if not isinstance(body, dict):
            body = {}

        url = body.get("data")

        if (
            response.ok
            and body.get("success")
            and isinstance(url, str)
            and url.startswith("http")
        ):
            # TorBox keeps handing out links on CDN servers it has pulled; their
            # names resolve to 0.0.0.0, so the file can't be streamed from here.
            if _host_offline(url):
                logger.warning(
                    f"TorBox torrent {torrent_id} file {file_id} is on an offline CDN server ({urlparse(url).hostname}); picking another source"
                )
                raise DebridServiceLinkUnavailable(
                    provider=self.key, link=link, reason="cdn_host_down"
                )

            return UnrestrictedLink(download=url, filename="file", filesize=0)

        # TorBox answers a vanished torrent or file with a generic 500
        # DATABASE_ERROR, so confirm against the account before giving up on it.
        if self._file_gone(torrent_id, file_id):
            logger.warning(
                f"TorBox torrent {torrent_id} file {file_id} is no longer on the account"
            )
            raise DebridServiceLinkUnavailable(provider=self.key, link=link)

        logger.warning(
            f"TorBox could not create a link for torrent {torrent_id} file {file_id}: {_error_text(response, body)}"
        )

        return None

    def get_user_info(self) -> UserInfo | None:
        """
        Get normalized user information from TorBox.

        Returns:
            UserInfo with normalized fields, or None on error.
        """

        try:
            response, body = self._request("GET", "user/me")

            if not response.ok or not body.get("success"):
                logger.error(
                    f"Failed to get TorBox user info: {_error_text(response, body)}"
                )
                return None

            data = body.get("data") or {}

            # plan: 0 Free, 1 Essential, 2 Pro, 3 Standard
            plan = int(data.get("plan") or 0)
            expires_at = _parse_time(data.get("premium_expires_at"))
            days_left = (
                max(0, (expires_at - datetime.now(tz=timezone.utc)).days)
                if expires_at
                else None
            )

            return UserInfo(
                service="torbox",
                email=data.get("email"),
                user_id=data.get("id") or 0,
                premium_status="premium" if plan > 0 else "free",
                premium_expires_at=expires_at,
                premium_days_left=days_left,
                total_downloaded_bytes=data.get("total_bytes_downloaded"),
                cooldown_until=_parse_time(data.get("cooldown_until")),
            )
        except Exception as e:
            logger.error(f"Error getting TorBox user info: {e}")
            return None
