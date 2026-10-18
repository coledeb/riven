"""An Ongoing season that was just scraped must send its aired episodes to scraping."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from kink import di

from program.media.item import Episode, Season
from program.media.state import States
from program.program import Program
from program.state_transition import process_event


class FakeScraping:
    def should_submit(self, _item):
        return True


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


def make_season(aired_scraped_at=None):
    season = Season({"number": 4})
    aired = Episode(
        {"number": 1, "title": "E1", "aired_at": datetime.now() - timedelta(days=2)}
    )
    upcoming = Episode(
        {"number": 2, "title": "E2", "aired_at": datetime.now() + timedelta(days=5)}
    )
    aired.scraped_at = aired_scraped_at
    season.episodes = [aired, upcoming]
    for episode in season.episodes:
        episode.parent = season
    season.scraped_at = datetime.now()
    season.scraped_times = 1
    for item in (aired, upcoming, season):
        item.store_state()
    return season, aired


def test_scraped_ongoing_season_queues_aired_episode(services):
    season, aired = make_season()
    assert season.last_state == States.Ongoing

    result = process_event(services.scraping, season)

    assert result.related_media_items == [aired]


def test_scraper_event_does_not_rescrape_scraped_episode(services):
    season, aired = make_season(aired_scraped_at=datetime.now())

    assert process_event(services.scraping, season).related_media_items == []
    assert process_event(services.scraping, aired).related_media_items == []
