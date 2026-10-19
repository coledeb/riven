"""Overseerr season requests: only the requested seasons of a show are fetched."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from kink import di

from program.apis.overseerr_api import OverseerrAPI
from program.media.item import Episode, Season, Show
from program.media.state import States
from program.program import Program
from program.services.indexers.tvdb_indexer import TVDBIndexer
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


def make_show(seasons: int = 3) -> Show:
    show = Show({"title": "Show", "tvdb_id": "1", "tvdb_status": "Continuing"})
    show.indexed_at = datetime.now()

    for number in range(1, seasons + 1):
        season = Season({"number": number})
        season.parent = show
        season.indexed_at = show.indexed_at
        episode = Episode(
            {
                "number": 1,
                "title": f"S{number}E1",
                "aired_at": datetime.now() - timedelta(days=30),
            }
        )
        episode.parent = season
        episode.indexed_at = show.indexed_at
        season.episodes = [episode]

    for season in show.seasons:
        season.episodes[0].store_state()
        season.store_state()
    show.store_state()

    return show


def test_unrequested_seasons_are_paused(services):
    show = make_show()

    TVDBIndexer._pause_unrequested_seasons(show, {3})

    states = [s.last_state for s in show.seasons]
    assert states[:2] == [States.Paused, States.Paused]
    assert states[2] != States.Paused
    assert show.seasons[0].episodes[0].last_state == States.Paused

    show.store_state()
    assert show.last_state == States.Indexed


@pytest.mark.parametrize("requested", [None, set(), {7}])
def test_all_seasons_kept_without_a_usable_request(services, requested):
    show = make_show()

    TVDBIndexer._pause_unrequested_seasons(show, requested)

    assert all(s.last_state != States.Paused for s in show.seasons)


def test_show_with_paused_seasons_scrapes_requested_seasons_only(services):
    show = make_show()
    TVDBIndexer._pause_unrequested_seasons(show, {2, 3})
    show.store_state()

    result = process_event("TraktIndexer", show)

    assert result.service is services.scraping
    assert result.related_media_items == show.seasons[1:]


def test_show_without_paused_seasons_still_scrapes_as_a_whole(services):
    show = make_show()

    result = process_event("TraktIndexer", show)

    assert result.related_media_items == [show]


def test_show_is_paused_only_when_every_season_is(services):
    show = make_show(2)
    show.seasons[0].store_state(States.Paused)
    show.store_state()
    assert show.last_state == States.Indexed

    show.seasons[1].store_state(States.Paused)
    show.store_state()
    assert show.last_state == States.Paused


class FakeResponse:
    ok = True

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def request(id, tvdb_id, seasons, media_status=3, type="tv"):
    return {
        "id": id,
        "status": 2,
        "type": type,
        "media": {"id": id, "tmdbId": id * 10, "tvdbId": tvdb_id, "status": media_status},
        "seasons": [{"id": n, "seasonNumber": n} for n in seasons],
    }


def get_requests(results, filter_pending_items=True):
    api = object.__new__(OverseerrAPI)
    api.session = SimpleNamespace(get=lambda _url: FakeResponse({"results": results}))
    return api.get_media_requests("overseerr", filter_pending_items=filter_pending_items)


def test_requests_carry_their_seasons():
    items = get_requests(
        [
            request(1, 100, [11, 12]),
            request(2, 100, [13]),
            request(3, 200, []),
            request(4, None, [], type="movie"),
        ]
    )

    assert [(i.tvdb_id, i.requested_seasons) for i in items] == [
        (100, {11, 12, 13}),
        (200, None),
        (None, None),
    ]


def test_whole_show_request_wins_over_a_season_request():
    items = get_requests([request(1, 100, [2]), request(2, 100, [])])

    assert [i.requested_seasons for i in items] == [None]


def test_partially_available_show_requests_are_kept():
    items = get_requests(
        [
            request(1, 100, [3], media_status=4),
            request(2, None, [], media_status=4, type="movie"),
            request(3, 300, [1], media_status=5),
        ]
    )

    assert [(i.tvdb_id, i.requested_seasons) for i in items] == [(100, {3})]
