"""Notification service for Riven"""

import json
import threading
from datetime import datetime
from pathlib import Path

from kink import di
from loguru import logger

from program.apis.overseerr_api import OverseerrAPI
from program.managers.sse_manager import sse_manager
from program.media.item import Episode, MediaItem, Season, Show
from program.media.state import States
from program.settings import settings_manager
from program.core.runner import Runner
from program.settings.models import NotificationsModel
from program.utils import data_dir_path
from program.utils.notify import GREEN, Notice, _tmdb_for_tvdb, item_title, send


def _completed_size(item: MediaItem) -> int:
    """How much of the item is complete: a show's completed episodes, else 1."""

    if isinstance(item, Show):
        return sum(
            1
            for season in item.seasons
            for episode in season.episodes
            if episode.last_state == States.Completed
        )

    return 1


def _already_completed() -> dict[str, int]:
    """Everything completed before the log existed, so it isn't announced again."""

    from sqlalchemy import text

    from program.db.db import db_session

    with db_session() as session:
        seen = {
            str(item_id): 1
            for (item_id,) in session.execute(
                text(
                    'SELECT id FROM "MediaItem" '
                    "WHERE type = 'movie' AND last_state = 'Completed'"
                )
            )
        }

        for show_id, episodes in session.execute(
            text(
                'SELECT s.parent_id, count(*) FROM "Episode" e '
                'JOIN "Season" s ON s.id = e.parent_id '
                'JOIN "MediaItem" ei ON ei.id = e.id '
                'JOIN "MediaItem" si ON si.id = s.parent_id '
                "WHERE ei.last_state = 'Completed' AND si.last_state = 'Completed' "
                "GROUP BY s.parent_id"
            )
        ):
            seen[str(show_id)] = episodes

    return seen


class CompletionLog:
    """
    What was announced as completed. A re-sourced file takes its item out of
    Completed and back, which announced the movie or the whole show again (once
    per episode state change for shows). Now it's announced only the first time,
    or for a show once it has more completed episodes than last time.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or data_dir_path / "announced_completions.json"
        self._lock = threading.Lock()
        self._seen: dict[str, int] | None = None

    def first(self, item: MediaItem) -> bool:
        """Whether this completion is new; records it if so."""

        try:
            size = _completed_size(item)
        except Exception as e:
            # e.g. episodes not loaded: still announce a show only once
            logger.debug(f"Couldn't count {item.log_string}'s completed episodes: {e}")
            size = 1

        with self._lock:
            seen = self._load()
            key = str(item.id)

            if seen.get(key, 0) >= size:
                return False

            seen[key] = size
            self._save()

        return True

    def _load(self) -> dict[str, int]:
        if self._seen is None:
            try:
                self._seen = json.loads(self._path.read_text())
            except FileNotFoundError:
                try:
                    self._seen = _already_completed()
                except Exception as e:
                    logger.warning(f"Couldn't read what's already completed: {e}")
                    self._seen = {}

                self._save()
            except ValueError:
                self._seen = {}

        return self._seen

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._seen))
        except OSError as e:
            logger.warning(f"Couldn't save {self._path.name}: {e}")


class NotificationService(Runner[NotificationsModel, None, None]):
    """
    Unified notification service that handles all notification types:
    - SSE state change notifications (real-time UI updates)
    - SSE completion notifications (frontend notification popups)
    - External notifications (Seerr-style Discord embeds, Apprise for the rest)
    """

    def __init__(self):
        super().__init__()

        self.initialized = False
        self.settings = settings_manager.settings.notifications
        self.completions = CompletionLog()
        self.initialized = True

    @classmethod
    def get_key(cls) -> str:
        return "notifications"

    def validate(self) -> bool:
        """Validate that the notification service is properly configured."""

        return True  # Service is always valid, even if no external services configured

    def run(
        self,
        item: MediaItem,
        *,
        previous_state: States | None = None,
        new_state: States | None = None,
    ):
        """
        Main entry point for sending notifications.

        Automatically determines what notifications to send based on the item's state:
        - If previous_state and new_state are provided: sends state change notification
        - If item.last_state is Completed: sends completion notification

        For episodes/seasons, also checks if the parent show is complete and notifies.

        Args:
            item: The MediaItem to notify about
            previous_state: Optional previous state (for state change notifications)
            new_state: Optional new state (for state change notifications)
        """

        # Handle state change notifications
        if previous_state is not None and new_state is not None:
            self._notify_state_change(item, previous_state, new_state)

        item_to_notify = item

        if isinstance(item, Episode):
            item_to_notify = item.parent.parent
        elif isinstance(item, Season):
            item_to_notify = item.parent

        # Handle completion notifications
        if item_to_notify.last_state == States.Completed:
            if self.completions.first(item_to_notify):
                self._notify_completion(item_to_notify)
            else:
                logger.debug(f"{item_to_notify.log_string} was already announced as completed")

    def _notify_state_change(
        self, item: MediaItem, previous_state: States, new_state: States
    ):
        """
        Notify about a state change via SSE.

        This is called automatically from MediaItem.store_state() to provide
        real-time updates to the frontend.

        Args:
            item: The MediaItem that changed state
            previous_state: The previous state
            new_state: The new state
        """
        if previous_state and previous_state != new_state:
            state_change_data = {
                "last_state": previous_state.name,
                "new_state": new_state.name,
                "item_id": item.id,
                "imdb_id": item.imdb_id,
                "tmdb_id": item.tmdb_id,
                "tvdb_id": item.tvdb_id,
            }
            sse_manager.publish_event("item_update", json.dumps(state_change_data))
            logger.debug(
                f"State change notification: {item.log_string} {previous_state.name} -> {new_state.name}"
            )

    def _notify_completion(self, item: MediaItem):
        """
        Notify about item completion via SSE and external services.

        This sends:
        1. Success log message
        2. SSE event for frontend notification popup
        3. External notifications (Discord, etc.) if enabled

        Args:
            item: The completed MediaItem
        """
        # Calculate completion duration

        assert item.requested_at

        duration = round((datetime.now() - item.requested_at).total_seconds())

        logger.success(f"{item.log_string} has been completed in {duration} seconds.")

        # Publish SSE notification event for frontend
        notification_data = {
            "title": item.title or "Unknown",
            "type": item.type,
            "year": item.aired_at.year if item.aired_at else None,
            "duration": duration,
            "timestamp": datetime.now().isoformat(),
            "log_string": item.log_string,
            "imdb_id": item.imdb_id,
        }

        sse_manager.publish_event("notifications", json.dumps(notification_data))

        logger.debug(f"SSE notification published for {item.log_string}")

        # Send external notifications if enabled
        if self.settings.enabled:
            self._send_external_notification(item)

    def _send_external_notification(self, item: MediaItem):
        """
        Send external notifications (Discord embeds, Apprise for the rest).

        Args:
            item: The MediaItem to notify about
        """
        # Check if this item type should trigger notifications
        if item.type not in self.settings.on_item_type:
            logger.debug(
                f"Skipping external notification for {item.type} (not in on_item_type)"
            )
            return

        if self.settings.skip_seerr_requests and self._seerr_announces(item):
            logger.debug(
                f"Skipping external notification for {item.log_string} (Seerr announces its request)"
            )
            return

        kind = item.type.title()
        notice = Notice(
            event=f"{kind} Now Available",
            title=item_title(item),
            description=f"Riven completed this {item.type}.",
            color=GREEN,
            fields=[("Requested Via", item.requested_by or "")],
        ).with_item(item, self.settings.link_url)

        send(self.settings.service_urls, notice)
        logger.debug(f"External notification sent for {item.log_string}")

    def _seerr_announces(self, item: MediaItem) -> bool:
        """
        Whether Seerr will post its own Media Available for this item.

        Asks Seerr whether the title has a request; if Seerr can't be asked,
        falls back to whether the item came from the Seerr content source.

        Args:
            item: The completed MediaItem
        """
        from_seerr = item.requested_by == "overseerr"

        if OverseerrAPI not in di:
            return from_seerr

        kind = "movie" if item.type == "movie" else "tv"
        tmdb_id = item.tmdb_id

        if not tmdb_id and kind == "tv" and item.tvdb_id:
            tmdb_id = _tmdb_for_tvdb(str(item.tvdb_id))

        if not tmdb_id:
            return from_seerr

        try:
            return di[OverseerrAPI].has_request(kind, str(tmdb_id))
        except Exception as e:
            logger.warning(f"Seerr request lookup failed for {item.log_string}: {e}")
            return from_seerr

    def _notify_generic(self, title: str, body: str):
        """
        Send a generic notification to all configured services.

        This is a private utility method for sending custom notifications that aren't
        tied to a specific MediaItem.

        Args:
            title: Notification title
            body: Notification body
        """
        if not self.settings.enabled:
            logger.debug("Notifications disabled, skipping generic notification")
            return

        send(self.settings.service_urls, Notice(event="Riven", title=title, description=body))
