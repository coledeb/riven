"""A season whose pack search found nothing must scrape its aired episodes one by one."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from kink import di

from program.media.item import Episode, Season, Show
from program.media.state import States
from program.program import Program
from program.services.scrapers import episode_pacer
from program.services.scrapers.episode_pacer import EpisodeFallbackPacer
from program.state_transition import process_event


class FakeScraping:
    def __init__(self):
        self.blocked = set()

    def should_submit(self, item):
        return id(item) not in self.blocked


class FakeEM:
    def __init__(self):
        self.queued = []

    def _id_in_queue(self, item_id):
        return any(e.item_id == item_id for e in self.queued)

    def add_event_to_queue(self, event, log_message=True):
        self.queued.append(event)


@pytest.fixture
def services():
    services = SimpleNamespace(
        scraping=FakeScraping(),
        indexer=object(),
        downloader=object(),
    )
    previous = di[Program] if Program in di else None
    di[Program] = SimpleNamespace(services=services, em=FakeEM())
    episode_pacer.pacer.reset()
    yield services
    episode_pacer.pacer.reset()
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


def make_long_season(count=25):
    show = Show({"title": "Long Show"})
    season = Season({"number": 1})
    season.parent = show
    season.id = 77
    episodes = []
    for ep in range(1, count + 1):
        episode = Episode(
            {"number": ep, "title": f"E{ep}", "aired_at": datetime.now() - timedelta(days=100 - ep)}
        )
        episode.parent = season
        episode.id = 1000 + ep
        episodes.append(episode)
    season.episodes = episodes
    show.seasons = [season]
    season.scraped_at = datetime.now()
    season.failed_attempts = 1
    for episode in season.episodes:
        episode.store_state()
    season.store_state()
    return season


def test_fallback_sends_a_batch_and_queues_the_season_for_the_rest(services):
    season = make_long_season()

    result = process_event("RetryLibrary", season)

    B = episode_pacer.BATCH_PER_SEASON
    assert result.related_media_items == season.episodes[:B]
    queued = di[Program].em.queued
    assert [e.item_id for e in queued] == [77]
    assert queued[0].run_at > datetime.now() + timedelta(seconds=50)

    # The delayed pass sends the next batch, not the episodes already sent
    result = process_event("StateTransition", season)
    assert result.related_media_items == season.episodes[B : 2 * B]
    assert len(di[Program].em.queued) == 1  # already queued, not doubled


def test_manual_scrape_is_not_paced(services):
    season = make_long_season()

    result = process_event("RetryLibrary", season, overrides={})

    assert result.related_media_items == season.episodes


def test_partly_done_failed_season_is_paced_too(services):
    season = make_long_season()
    season.episodes[0].store_state(States.Completed)
    season.last_state = States.PartiallyCompleted  # as Riven stores it

    result = process_event("RetryLibrary", season)

    B = episode_pacer.BATCH_PER_SEASON
    assert result.related_media_items == season.episodes[1 : B + 1]
    assert [e.item_id for e in di[Program].em.queued] == [77]


def test_pacer_caps_episodes_per_window_across_seasons():
    now = [0.0]
    pacer = EpisodeFallbackPacer(clock=lambda: now[0])
    sent = 0
    for season in range(20):
        grant = pacer.take(
            [(season, ep) for ep in range(episode_pacer.BATCH_PER_SEASON)],
            key=lambda e: e,
        )
        sent += len(grant.allowed)
    assert sent == episode_pacer.MAX_PER_WINDOW
    assert grant.retry_in > episode_pacer.RETRY_SECONDS

    now[0] += episode_pacer.WINDOW_SECONDS
    grant = pacer.take([(99, 0)], key=lambda e: e)
    assert grant.allowed == [(99, 0)] and grant.retry_in is None
