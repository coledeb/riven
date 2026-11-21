from typing import Any, Literal
from kink import di
from loguru import logger

from program.media import MediaItem, States
from program.types import ProcessedEvent, Service
from program.media.item import Episode, Season, Show


def process_event(
    emitted_by: Service | Literal["StateTransition", "RetryLibrary"] | str,
    existing_item: MediaItem | None = None,
    content_item: MediaItem | None = None,
    overrides: dict[str, Any] | None = None,
) -> ProcessedEvent:
    """Process an event and return the updated item, next service and items to submit."""

    from program.program import Program

    services = di[Program].services

    assert services

    next_service: Service | None = None
    no_further_processing = ProcessedEvent(
        service=None,
        related_media_items=[],
    )
    items_to_submit = list[MediaItem]()

    if existing_item and existing_item.last_state in [States.Paused, States.Failed]:
        return no_further_processing

    if content_item or (existing_item and existing_item.last_state == States.Requested):
        log_string = None

        if existing_item:
            log_string = existing_item.log_string
        elif content_item:
            log_string = content_item.log_string

        logger.debug(f"Submitting {log_string} to IndexerService")

        related_media_items = list[MediaItem]()

        if content_item:
            related_media_items.append(content_item)
        elif existing_item:
            related_media_items.append(existing_item)

        return ProcessedEvent(
            service=services.indexer,
            related_media_items=related_media_items,
            overrides=overrides,
        )

    elif existing_item and existing_item.last_state in [
        States.PartiallyCompleted,
        States.Ongoing,
    ]:
        if isinstance(existing_item, Show):
            incomplete_seasons = [
                s
                for s in existing_item.seasons
                if s.last_state not in [States.Completed, States.Unreleased]
            ]

            for season in incomplete_seasons:
                processed_event = process_event(
                    emitted_by, season, None, overrides
                )

                if processed_event.related_media_items:
                    items_to_submit += processed_event.related_media_items
        elif isinstance(existing_item, Season):
            incomplete_episodes = [
                e for e in existing_item.episodes if e.last_state != States.Completed
            ]

            for episode in incomplete_episodes:
                processed_event = process_event(
                    emitted_by, episode, None, overrides
                )

                if processed_event.related_media_items:
                    items_to_submit += processed_event.related_media_items

            if _pack_search_failed(existing_item) and overrides is None:
                # Episodes headed for the scraper go out at the fallback pace
                to_scrape = [
                    e
                    for e in items_to_submit
                    if e.last_state in [States.Indexed, States.Unknown]
                ]
                if to_scrape:
                    held = {id(e) for e in to_scrape} - {
                        id(e) for e in _pace(existing_item, to_scrape)
                    }
                    items_to_submit = [
                        e for e in items_to_submit if id(e) not in held
                    ]

    elif existing_item and existing_item.last_state in [States.Indexed, States.Unknown]:
        next_service = services.scraping

        # A scraper event only blocks re-scraping the item it just scraped.
        # Episodes of a just-scraped Ongoing season were never scraped
        # themselves, so they still go to the scraper.
        never_scraped_episode = (
            isinstance(existing_item, Episode) and existing_item.scraped_at is None
        )

        # A show with paused seasons is scraped per season, so a whole-show
        # pack can't fill the paused ones
        has_paused_seasons = isinstance(existing_item, Show) and any(
            s.last_state == States.Paused for s in existing_item.seasons
        )

        if (
            (emitted_by != services.scraping or never_scraped_episode)
            and not has_paused_seasons
            and (
                overrides is not None
                or services.scraping.should_submit(existing_item)
            )
        ):
            items_to_submit = _scrape_targets(existing_item, overrides)
        elif isinstance(existing_item, Show):
            # Decompose Show → Seasons (each season scrapes for packs)
            for s in existing_item.seasons:
                if s.last_state in [
                    States.Indexed,
                    States.PartiallyCompleted,
                    States.Unknown,
                ] and (
                    overrides is not None
                    or _pack_search_failed(s)
                    or services.scraping.should_submit(s)
                ):
                    items_to_submit += _scrape_targets(s, overrides)
        elif isinstance(existing_item, Season):
            # Keep the Season as a unit while season packs are still being
            # tried; once a pack search has found nothing, scrape its episodes.
            if _pack_search_failed(existing_item) or services.scraping.should_submit(
                existing_item
            ):
                items_to_submit = _scrape_targets(existing_item, overrides)

    elif existing_item and existing_item.last_state == States.Scraped:
        next_service = services.downloader
        items_to_submit = [existing_item]

    elif existing_item and existing_item.last_state == States.Downloaded:
        next_service = services.filesystem
        items_to_submit = [existing_item]

    elif existing_item and existing_item.last_state == States.Symlinked:
        next_service = services.updater
        items_to_submit = [existing_item]

    elif existing_item and existing_item.last_state == States.Completed:
        # Avoid multiple post-processing runs
        if emitted_by != services.post_processing:
            next_service = services.post_processing
            items_to_submit = [existing_item]
        else:
            return no_further_processing

    if items_to_submit:
        service_name = (
            next_service.__class__.__name__ if next_service else "StateTransition"
        )

        logger.debug(
            f"State transition complete: {len(items_to_submit)} items queued for {service_name}"
        )

    return ProcessedEvent(
        service=next_service,
        related_media_items=items_to_submit,
        overrides=overrides,
    )


def _pack_search_failed(item: MediaItem) -> bool:
    """A season whose pack search has come back with no new streams."""

    return isinstance(item, Season) and (item.failed_attempts or 0) > 0


def _scrape_targets(
    item: MediaItem, overrides: dict[str, Any] | None
) -> list[MediaItem]:
    """What to scrape for an item.

    A season scrape only accepts complete season packs. When none was found,
    scrape the aired episodes one by one instead: an episode scrape still
    accepts packs, and also single-episode releases (shows that are only
    released per episode would otherwise never be found).
    """

    if not _pack_search_failed(item):
        return [item]

    from program.program import Program

    program = di[Program]
    scraping = program.services.scraping
    season = item
    assert isinstance(season, Season)

    due = [
        e
        for e in season.episodes
        if e.last_state in [States.Indexed, States.Unknown]
        and e.is_released
        and (overrides is not None or scraping.should_submit(e))
    ]

    # A manual scrape isn't paced
    if overrides is not None or not due:
        return due

    return _pace(season, due)


def _pace(season: Season, due: list[MediaItem]) -> list[MediaItem]:
    """Let a few of a failed season's episodes through; queue the rest for later."""

    from program.program import Program
    from program.services.scrapers.episode_pacer import pacer

    program = di[Program]

    grant = pacer.take(
        due,
        playback_active=_playback_active(program),
        key=lambda e: e.id if e.id is not None else id(e),
    )

    if grant.retry_in is not None:
        _retry_season_later(program, season, grant.retry_in)

    return grant.allowed


def _playback_active(program: Any) -> bool:
    """Whether the VFS is serving any file (Plex playing or analysing)."""

    filesystem = getattr(program.services, "filesystem", None)
    vfs = filesystem and getattr(filesystem, "riven_vfs", None)

    return bool(vfs and vfs.has_active_streams())


def _retry_season_later(program: Any, season: Season, delay: float) -> None:
    """Queue the season again so its next episodes go out after `delay`."""

    from datetime import datetime, timedelta

    from program.types import Event

    em = getattr(program, "em", None)

    if em is None or season.id is None or em._id_in_queue(season.id):
        return

    em.add_event_to_queue(
        Event(
            emitted_by="StateTransition",
            item_id=season.id,
            run_at=datetime.now() + timedelta(seconds=delay),
        ),
        log_message=False,
    )
    logger.debug(
        f"Episode fallback paced: {season.log_string} continues in {int(delay)}s"
    )
