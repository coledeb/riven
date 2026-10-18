"""Overseerr API client"""

from typing import TYPE_CHECKING, Literal

from loguru import logger
from requests.exceptions import ConnectionError, RetryError
from urllib3.exceptions import MaxRetryError, NewConnectionError

from program.utils.request import SmartSession, get_hostname_from_url

if TYPE_CHECKING:
    from program.media.item import MediaItem

type ItemType = Literal["tv", "movie"]


class OverseerrAPIError(Exception):
    """Base exception for OverseerrAPI related errors"""


class OverseerrAPI:
    """Handles Overseerr API communication"""

    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

        self.session = SmartSession(
            base_url=base_url,
            rate_limits={
                # 1000 calls per 5 minutes, retries=3, backoff_factor=0.3
                get_hostname_from_url(self.base_url): {
                    "rate": 1000 / 300,
                    "capacity": 1000,
                }
            },
        )

        self.session.headers.update(
            {
                "X-Api-Key": self.api_key,
            }
        )

    def validate(self):
        """Validate API connection"""

        try:
            return self.session.get("api/v1/auth/me", timeout=15).ok
        except (ConnectionError, RetryError, MaxRetryError, NewConnectionError):
            logger.error("Overseerr URL is not reachable, or it timed out")
        except Exception as e:
            logger.error(f"Unexpected error during Overseerr validation: {str(e)}")

        return False

    def get_media_requests(
        self,
        service_key: str,
        filter: (
            Literal[
                "all",
                "approved",
                "available",
                "pending",
                "processing",
                "unavailable",
                "failed",
                "deleted",
                "completed",
            ]
            | None
        ) = "approved",
        take: int = 10000,
        filter_pending_items: bool = True,
    ) -> list["MediaItem"]:
        """Get media requests from `Overseerr`"""

        from program.media.item import MediaItem

        url = f"api/v1/request?take={take}&sort=added"

        if filter:
            url += f"&filter={filter}"

        try:
            response = self.session.get(url)

            if not response.ok:
                logger.error(f"Failed to get response from overseerr: {response.data}")

                return []

            from schemas.overseerr import UserUserIdRequestsGet200Response

            response_data = UserUserIdRequestsGet200Response.from_dict(response.json())

            assert response_data

            if not response_data.results:
                logger.debug("No user approved requests found from overseerr")

                return []

        except Exception as e:
            logger.error(f"Failed to get response from overseerr: {str(e)}")

            return []

        # Lets look at approved items only that are only in the pending state
        pending_items = response_data.results

        if filter_pending_items and filter == "approved":
            pending_items = [
                item
                for item in response_data.results
                if item.status == 2 and item.media and item.media.status == 3
            ]

        media_items: list[MediaItem] = []

        for item in pending_items:
            tmdb_id = item.media and item.media.tmdb_id
            tvdb_id = item.media and item.media.tvdb_id

            if tvdb_id is not None:
                media_items.append(
                    MediaItem(
                        {
                            "tvdb_id": tvdb_id,
                            "requested_by": service_key,
                        }
                    )
                )

                continue

            if tmdb_id is not None:
                media_items.append(
                    MediaItem(
                        {
                            "tmdb_id": tmdb_id,
                            "requested_by": service_key,
                        }
                    )
                )

                continue

            logger.error(f"Could not determine ID for overseerr item: {item.id}")

        return media_items

    def media_ids(self) -> dict[tuple[str, str], int]:
        """Map ("movie"|"tv", tmdb id) and ("tvdb", tvdb id) to Overseerr media ids."""

        ids = dict[tuple[str, str], int]()
        skip = 0

        while True:
            response = self.session.get(
                f"api/v1/media?take=100&skip={skip}&filter=all", timeout=30
            )

            if not response.ok:
                raise OverseerrAPIError(f"media list failed: {response.status_code}")

            results = response.json().get("results") or []

            for media in results:
                if media.get("tmdbId"):
                    ids[(media["mediaType"], str(media["tmdbId"]))] = media["id"]

                if media.get("mediaType") == "tv" and media.get("tvdbId"):
                    ids[("tvdb", str(media["tvdbId"]))] = media["id"]

            if len(results) < 100:
                return ids

            skip += 100

    def _as_user(self, user_id: int) -> dict[str, str]:
        return {"X-Api-User": str(user_id)} if user_id else {}

    def create_issue(
        self,
        media_id: int,
        message: str,
        season: int = 0,
        episode: int = 0,
        user_id: int = 0,
    ) -> int:
        """Open an issue ("other" type) and return its id."""

        response = self.session.post(
            "api/v1/issue",
            json={
                "issueType": 4,
                "message": message,
                "mediaId": media_id,
                "problemSeason": season,
                "problemEpisode": episode,
            },
            headers=self._as_user(user_id),
            timeout=30,
        )

        if not response.ok:
            raise OverseerrAPIError(f"issue create failed: {response.status_code}")

        return response.json()["id"]

    def comment_issue(self, issue_id: int, message: str, user_id: int = 0) -> bool:
        """Comment on an issue. False if the issue no longer exists."""

        response = self.session.post(
            f"api/v1/issue/{issue_id}/comment",
            json={"message": message},
            headers=self._as_user(user_id),
            timeout=30,
        )

        if response.status_code == 404:
            return False

        if not response.ok:
            raise OverseerrAPIError(f"issue comment failed: {response.status_code}")

        return True

    def resolve_issue(self, issue_id: int, user_id: int = 0) -> bool:
        """Mark an issue resolved. False if the issue no longer exists."""

        response = self.session.post(
            f"api/v1/issue/{issue_id}/resolved",
            json={},
            headers=self._as_user(user_id),
            timeout=30,
        )

        if response.status_code == 404:
            return False

        if not response.ok:
            raise OverseerrAPIError(f"issue resolve failed: {response.status_code}")

        return True

    def delete_request(self, mediaId: int) -> bool:
        """Delete request from Overseerr"""

        try:
            response = self.session.delete(f"api/v1/request/{mediaId}")

            logger.debug(f"Deleted request {mediaId} from Overseerr")

            return response.ok
        except Exception as e:
            logger.error(f"Failed to delete request from Overseerr: {str(e)}")

            return False


# Statuses for Media Requests endpoint /api/v1/request:
# item.status:
# 1 = PENDING APPROVAL,
# 2 = APPROVED,
# 3 = DECLINED

# Statuses for Media Info endpoint /api/v1/media:
# item.media.status:
# 1 = UNKNOWN,
# 2 = PENDING,
# 3 = PROCESSING,
# 4 = PARTIALLY_AVAILABLE,
# 5 = AVAILABLE
