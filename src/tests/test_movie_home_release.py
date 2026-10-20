"""A movie counts as released on its home (digital/physical/TV) release, not in cinemas."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from program.media.item import Movie
from program.media.state import States
from program.services.indexers.tmdb_indexer import movie_available_date
from program.scheduling.scheduler import ProgramScheduler

NOW = datetime(2026, 10, 19, 12, 0)


def _settings(wait=True, fallback_days=60):
    return SimpleNamespace(
        movie_wait_for_home_release=wait,
        movie_home_release_fallback_days=fallback_days,
    )


def _details(release_date, releases):
    """releases: [(country, type, 'YYYY-MM-DD'), ...]"""

    by_country: dict[str, list] = {}
    for country, kind, date in releases:
        by_country.setdefault(country, []).append(
            SimpleNamespace(type=kind, release_date=f"{date}T00:00:00.000Z")
        )

    return SimpleNamespace(
        release_date=release_date,
        release_dates=SimpleNamespace(
            results=[
                SimpleNamespace(iso_3166_1=c, release_dates=r)
                for c, r in by_country.items()
            ]
        ),
    )


# Street Fighter (2026), as TMDB had it on 2026-10-19
STREET_FIGHTER = _details(
    "2026-10-13",
    [("FR", 2, "2026-10-13"), ("US", 3, "2026-10-16"), ("US", 4, "2026-12-01")],
)


def test_in_cinemas_waits_for_digital_release():
    assert movie_available_date(STREET_FIGHTER, _settings(), NOW) == datetime(2026, 12, 1)


def test_setting_off_keeps_cinema_date():
    assert movie_available_date(STREET_FIGHTER, _settings(wait=False), NOW) == datetime(
        2026, 10, 13
    )


def test_earliest_home_release_in_any_country():
    details = _details(
        "2026-08-01",
        [("US", 3, "2026-08-01"), ("US", 4, "2026-10-30"), ("GB", 5, "2026-10-01")],
    )
    assert movie_available_date(details, _settings(), NOW) == datetime(2026, 10, 1)


def test_no_home_date_uses_fallback_after_first_cinema_release():
    # Premieres (type 1) don't count as the cinema release
    details = _details(
        "2026-09-10", [("US", 1, "2026-05-01"), ("US", 3, "2026-09-10")]
    )
    assert movie_available_date(details, _settings(fallback_days=60), NOW) == datetime(
        2026, 11, 9
    )


def test_old_cinema_release_counts_as_released():
    # Only a late 4K reissue listed: still released long ago
    details = _details("1994-09-23", [("US", 3, "1994-09-23"), ("US", 5, "2027-03-01")])
    assert movie_available_date(details, _settings(), NOW) == datetime(1994, 9, 23)


def test_direct_to_streaming():
    details = _details("2026-11-05", [("US", 4, "2026-11-05")])
    assert movie_available_date(details, _settings(), NOW) == datetime(2026, 11, 5)


def test_no_dates_at_all():
    assert movie_available_date(_details(None, []), _settings(), NOW) is None


def _scheduler_with_reindexed(movie):
    program = MagicMock()
    program.services.indexer.run.return_value = iter(
        [SimpleNamespace(media_items=[movie])]
    )
    scheduler = ProgramScheduler.__new__(ProgramScheduler)
    scheduler.program = program
    session = MagicMock()
    session.merge.side_effect = lambda m: m
    return scheduler, program, session


def _movie(aired_at, last_state):
    movie = Movie({"title": "Street Fighter", "tmdb_id": "1153576", "aired_at": aired_at})
    movie.last_state = last_state
    return movie


def test_reindex_into_the_future_makes_indexed_movie_unreleased():
    movie = _movie(datetime(2099, 12, 1), States.Indexed)
    scheduler, program, session = _scheduler_with_reindexed(movie)

    scheduler._run_reindex_for_item(session, movie)

    assert movie.last_state == States.Unreleased
    program.em.add_event.assert_not_called()


def test_reindex_once_released_enqueues_movie():
    movie = _movie(datetime(2020, 1, 1), States.Unreleased)
    scheduler, program, session = _scheduler_with_reindexed(movie)

    scheduler._run_reindex_for_item(session, movie)

    assert movie.last_state == States.Indexed
    program.em.add_event.assert_called_once()
