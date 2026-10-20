"""Notification service for Riven"""

import json
from datetime import datetime

from loguru import logger

from program.managers.sse_manager import sse_manager
from program.media.item import Episode, MediaItem, Season
from program.media.state import States
from program.settings import settings_manager
from program.core.runner import Runner
from program.settings.models import NotificationsModel
from program.utils.notify import GREEN, Notice, item_title, send


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
            self._notify_completion(item_to_notify)

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
