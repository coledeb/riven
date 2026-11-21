"""A season whose pack search found nothing must scrape its aired episodes one by one."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from kink import di

from program.media.item import Episode, Season, Show
from program.media.state import States
from program.program import Program
from program.state_transition import process_event


class FakeScraping:
    def __init__(self):
        self.blocked = set()

    def should_submit(self, item):
        return id(item) not in self.blocked


@pytest.fixture
def services():
    services = SimpleNamespace(
        scraping=FakeScraping(), indexer=object(), downloader=object()
    )
    previous = di[Program] if Program in di else None
    di[Program] = SimpleNamespace(services=services)
    yield services
    if previous is not None:
        di[Program] = previous


def make_show(failed_attempts=0, done_season=False):
    show = Show({"title": "Cooking Show"})
    seasons = []
    for number in (1, 2):
        season = Season({"number": number})
        season.parent = show
        episodes = []
        for ep in (1, 2, 3):
            aired = datetime.now() - timedelta(days=30 - ep)
            episode = Episode({"number": ep, "title": f"E{ep}", "aired_at": aired})
            episode.parent = season
            episodes.append(episode)
        season.episodes = episodes
        season.scraped_at = datetime.now()
        season.scraped_times = failed_attempts
        season.failed_attempts = failed_attempts
        seasons.append(season)
    show.seasons = seasons
    if done_season:
        for episode in seasons[0].episodes:
            episode.store_state(States.Completed)
    for season in seasons:
        for episode in season.episodes:
            if episode.last_state != States.Completed:
                episode.store_state()
        season.store_state()
    if done_season:
        seasons[0].last_state = States.Completed
        show.last_state = States.PartiallyCompleted  # store_state would cascade
    else:
        show.store_state()
    return show


def test_untried_season_scrapes_as_pack(services):
    season = make_show().seasons[0]
    assert season.last_state == States.Indexed

    result = process_event("RetryLibrary", season)

    assert result.related_media_items == [season]


def test_failed_pack_search_falls_back_to_episodes(services):
    season = make_show(failed_attempts=1).seasons[0]

    result = process_event(services.scraping, season)

    assert result.related_media_items == season.episodes


def test_fallback_skips_unaired_and_not_due_episodes(services):
    season = make_show(failed_attempts=2).seasons[0]
    season.episodes[1].aired_at = datetime.now() + timedelta(days=3)
    services.scraping.blocked.add(id(season.episodes[2]))
    services.scraping.blocked.add(id(season))

    result = process_event("RetryLibrary", season)

    assert result.related_media_items == [season.episodes[0]]


def test_partial_show_retry_reaches_failed_season_episodes(services):
    show = make_show(failed_attempts=3, done_season=True)
    assert show.last_state == States.PartiallyCompleted

    result = process_event("RetryLibrary", show)

    assert result.related_media_items == show.seasons[1].episodes
