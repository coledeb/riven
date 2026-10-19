"""Episode scraping must only accept torrents for that episode's season."""

from types import SimpleNamespace

import pytest
from RTN import parse

from program.services.scrapers.shared import _check_episode_numbers


def episode(season, number, absolute_number=None):
    return SimpleNamespace(
        parent=SimpleNamespace(number=season),
        number=number,
        absolute_number=absolute_number,
    )


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Bobs.Burgers.S15E12.1080p.WEB.h264-ETHEL", True),
        ("Bobs.Burgers.S13E12.1080p.WEB.h264-ETHEL", False),
        ("Bobs.Burgers.S15.1080p.WEB.h264-ETHEL", True),
        ("Bobs.Burgers.S14.1080p.WEB.h264-ETHEL", False),
        ("Bobs.Burgers.S15E11.1080p.WEB.h264-ETHEL", False),
    ],
)
def test_season_and_episode_must_match(title, expected):
    assert _check_episode_numbers(episode(15, 12, 283), parse(title)) is expected


def test_absolute_number_matches_without_season():
    assert _check_episode_numbers(
        episode(3, 5, 49), parse("[SubsPlease] Show - 49 (1080p)")
    )


def test_absolute_equal_to_number_still_needs_the_season():
    assert not _check_episode_numbers(episode(1, 5, 5), parse("Show.S03E05.1080p.WEB"))
