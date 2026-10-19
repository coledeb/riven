"""Overseerr content module"""

from typing import cast

from kink import di
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from program.apis.overseerr_api import OverseerrAPI
from program.core.runner import MediaItemGenerator, Runner, RunnerResult
from program.db.db import db_session
from program.db.db_functions import item_exists_by_any_id
from program.media.item import MediaItem, Season, Show
from program.media.state import States
from program.settings import settings_manager
from program.settings.models import OverseerrModel


class Overseerr(Runner[OverseerrModel]):
    """Content class for overseerr"""

    is_content_service = True

    def __init__(self):
        super().__init__()

        self.settings = settings_manager.settings.content.overseerr

        if not self.enabled:
            return

        self.api = di[OverseerrAPI]
        self.initialized = self.validate()
        self.run_once = False
        self._resumed = set[tuple[str, frozenset[int]]]()

        if not self.initialized:
            return

        logger.success("Overseerr initialized!")

    def validate(self) -> bool:
        if not self.settings.enabled:
            return False

        if self.settings.api_key == "":
            logger.error("Overseerr API key is not set.")
            return False

        if len(self.settings.api_key) != 68:
            logger.error("Overseerr API key length is invalid.")
            return False

        try:
            return self.api.validate()
        except Exception:
            return False

    def run(self, item: MediaItem) -> MediaItemGenerator:
        """Fetch new media from `Overseerr`"""

        if self.settings.use_webhook and self.run_once:
            return

        overseerr_items = self.api.get_media_requests(
            self.key,
            filter_pending_items=self.run_once,
        )

        if self.settings.use_webhook:
            logger.info(
                "Webhook is enabled. Running Overseerr once before switching to webhook only mode"
            )

        self.run_once = True

        if overseerr_items:
            existing = [
                item
                for item in overseerr_items
                if item_exists_by_any_id(tvdb_id=item.tvdb_id, tmdb_id=item.tmdb_id)
            ]
            overseerr_items = [
                item
                for item in overseerr_items
                if not any(item is e for e in existing)
            ]

            self._resume_requested_seasons(existing)

        logger.info(f"Fetched {len(overseerr_items)} items from overseerr")

        yield RunnerResult(media_items=overseerr_items)

    def _resume_requested_seasons(self, items: list[MediaItem]) -> None:
        """Resume paused seasons that a newer request asks for."""

        from program.program import Program
        from program.types import Event
        from routers.secure.items import apply_item_mutation

        for item in items:
            if not item.tvdb_id or not item.requested_seasons:
                continue

            key = (str(item.tvdb_id), frozenset(item.requested_seasons))

            # Once per request: a season paused by hand later stays paused
            if key in self._resumed:
                continue

            self._resumed.add(key)

            with db_session() as session:
                show = session.execute(
                    select(Show).where(Show.tvdb_id == str(item.tvdb_id))
                ).scalar_one_or_none()

                if not show:
                    continue

                seasons = [
                    s
                    for s in show.seasons
                    if s.number in item.requested_seasons
                    and s.last_state == States.Paused
                ]

                if not seasons:
                    continue

                def mutation(season: MediaItem, s: Session) -> None:  # noqa: ARG001
                    # Recompute each episode's real state (Indexed, Unreleased, ...)
                    for episode in cast(Season, season).episodes:
                        episode.store_state()

                for season in seasons:
                    apply_item_mutation(di[Program], session, season, mutation)

                session.commit()

                di[Program].em.add_event(Event("RetryItem", show.id))

                logger.info(
                    f"Overseerr: resumed seasons {', '.join(str(s.number) for s in seasons)} of {show.log_string}"
                )
