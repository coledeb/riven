"""A stream that adds nothing because another download already filled every
episode (e.g. a multi-season pack fetched for a sibling season) must not be
blacklisted as wrong files."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from program.media.item import Episode, Movie, Season, Show
from program.media.state import States
from program.media.stream import BlacklistReason
from program.services.downloaders import Downloader


@pytest.fixture(autouse=True)
def _fake_files():
    """ORM relationships won't take mocks: a plain attribute stands in."""

    with patch.object(
        Episode,
        "filesystem_entry",
        property(lambda self: getattr(self, "_file", None)),
    ):
        yield


def _show(seasons: dict[int, int]) -> Show:
    show = Show({"imdb_id": "tt0098904", "requested_by": "user", "title": "Seinfeld"})

    for number, episodes in seasons.items():
        season = Season({"number": number})

        for e in range(1, episodes + 1):
            season.add_episode(Episode({"number": e}))

        show.add_season(season)

    return show


def _fill(season: Season) -> None:
    for episode in season.episodes:
        episode._file = Mock()


def test_season_covered_only_when_every_episode_has_a_file():
    show = _show({1: 2, 2: 2})
    season = show.seasons[1]

    assert Downloader._already_covered(season) is False

    season.episodes[0]._file = Mock()
    assert Downloader._already_covered(season) is False

    _fill(season)
    assert Downloader._already_covered(season) is True


def test_show_ignores_paused_seasons_and_episodes():
    show = _show({1: 2, 2: 2})
    _fill(show.seasons[0])

    assert Downloader._already_covered(show) is False

    show.seasons[1].last_state = States.Paused
    assert Downloader._already_covered(show) is True


def test_empty_season_and_movie_are_never_covered():
    assert Downloader._already_covered(Season({"number": 1})) is False
    assert Downloader._already_covered(Movie({"title": "Inception"})) is False


def test_episode_covered_by_its_own_file():
    show = _show({1: 1})
    episode = show.seasons[0].episodes[0]

    assert Downloader._already_covered(episode) is False

    episode._file = Mock()
    assert Downloader._already_covered(episode) is True


def _downloader() -> Downloader:
    downloader = Downloader.__new__(Downloader)
    service = Mock(key="torbox")
    downloader.initialized_services = [service]
    downloader.service = service
    downloader.initialized = True
    downloader._service_cooldowns = {}
    downloader.subtitles_enabled = False
    return downloader


def _run(downloader: Downloader, season: Season, streams: list, update):
    season.blacklist_stream = Mock()

    with (
        patch.object(downloader, "_uncached_downloads", return_value=None),
        patch.object(downloader, "validate_stream_on_service", return_value=Mock()),
        patch.object(
            downloader,
            "download_cached_stream_on_service",
            return_value=SimpleNamespace(id="123"),
        ),
        patch.object(downloader, "update_item_attributes", side_effect=update),
        patch(
            "program.services.downloaders.sort_streams_by_quality",
            return_value=streams,
        ),
    ):
        return list(downloader.run(season))


def test_pack_filled_by_sibling_season_is_not_blacklisted():
    show = _show({1: 2, 2: 2})
    season = show.seasons[1]
    _fill(season)
    streams = [Mock(infohash="aa"), Mock(infohash="bb")]
    downloader = _downloader()

    calls = []
    results = _run(
        downloader, season, streams, lambda *_, **__: calls.append(1) and False
    )

    season.blacklist_stream.assert_not_called()
    # Stops after the first stream: the season needs nothing more
    assert len(calls) == 1
    assert len(results) == 1
    assert results[0].media_items == [season]


def test_genuinely_wrong_pack_is_still_blacklisted():
    show = _show({1: 2, 2: 2})
    season = show.seasons[1]
    streams = [Mock(infohash="aa")]
    downloader = _downloader()

    _run(downloader, season, streams, lambda *_, **__: False)

    season.blacklist_stream.assert_called_once_with(
        streams[0], BlacklistReason.WRONG_FILES
    )
