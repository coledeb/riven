"""TorBox account import: matching torrent names, gap checks and safety limits."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from kink import di

from program.apis.tmdb_api import TMDBApi
from program.media.item import Episode, Movie, Season, Show
from program.media.state import States
from program.media.stream import Stream
from program.services.downloaders import torbox_import
from program.services.downloaders.torbox import TorBoxNotFound
from program.services.downloaders.torbox_import import Match, TorBoxAccountImport


class FakeTMDB:
    def __init__(self, movies=(), shows=(), tvdb=None):
        self.results = {"movie": list(movies), "tv": list(shows)}
        self.tvdb = tvdb
        self.searches = []

    def search(self, kind, query, year=None):
        self.searches.append((kind, query, year))
        return self.results[kind]

    def get_tvdb_id(self, _tmdb_id):
        return self.tvdb


@pytest.fixture
def importer(tmp_path, monkeypatch):
    importer = TorBoxAccountImport()
    importer._state_file = tmp_path / "torbox_import.json"
    importer.sent = []
    monkeypatch.setattr(importer, "_notify", importer.sent.append)
    return importer


@pytest.fixture
def tmdb():
    previous = di[TMDBApi] if TMDBApi in di else None
    yield lambda fake: di.__setitem__(TMDBApi, fake)
    if previous is not None:
        di[TMDBApi] = previous


def test_tmdb_movie_needs_exact_title_and_year(importer, tmdb):
    tmdb(
        FakeTMDB(
            movies=[
                {
                    "id": 1,
                    "title": "The Ring",
                    "release_date": "2002-10-18",
                    "popularity": 30,
                },
                {
                    "id": 2,
                    "title": "The Ring",
                    "release_date": "1927-10-01",
                    "popularity": 2,
                },
                {
                    "id": 3,
                    "title": "The Ring Two",
                    "release_date": "2005-03-18",
                    "popularity": 20,
                },
            ]
        )
    )

    match = importer._match_tmdb("movie", "The Ring", 2002)

    assert match == Match(kind="movie", title="The Ring", tmdb_id="1")


def test_tmdb_ambiguous_title_is_not_guessed(importer, tmdb):
    tmdb(
        FakeTMDB(
            movies=[
                {
                    "id": 1,
                    "title": "Wednesday",
                    "release_date": "2016-01-01",
                    "popularity": 5,
                },
                {
                    "id": 2,
                    "title": "Wednesday",
                    "release_date": "2021-01-01",
                    "popularity": 4,
                },
            ]
        )
    )

    assert isinstance(importer._match_tmdb("movie", "Wednesday", None), str)


def test_tmdb_show_needs_a_tvdb_id(importer, tmdb):
    show = {
        "id": 9,
        "name": "Lanterns",
        "first_air_date": "2026-08-17",
        "popularity": 50,
    }

    tmdb(FakeTMDB(shows=[show], tvdb="123"))
    assert importer._match_tmdb("show", "Lanterns", 2026) == Match(
        kind="show", title="Lanterns", tmdb_id="9", tvdb_id="123"
    )

    tmdb(FakeTMDB(shows=[show], tvdb=None))
    assert isinstance(importer._match_tmdb("show", "Lanterns", 2026), str)


def make_show():
    show = Show({"title": "Show", "tvdb_id": "1"})
    season = Season({"number": 1})
    for number in (1, 2):
        episode = Episode({"number": number, "title": f"E{number}"})
        season.episodes.append(episode)
    show.seasons.append(season)
    return show


def pack(*names, infohash="a" * 40):
    return {
        "id": 5,
        "hash": infohash,
        "name": "Show S01",
        "files": [{"id": i, "short_name": n} for i, n in enumerate(names)],
    }


def test_show_gap_only_for_episodes_without_a_file():
    show = make_show()
    e1, e2 = show.seasons[0].episodes
    e1.last_state = States.Completed
    e2.last_state = States.Indexed

    has_gap = TorBoxAccountImport._has_gap

    assert has_gap(show, pack("Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv"))
    assert not has_gap(show, pack("Show.S01E01.1080p.mkv"))

    dead = Stream.__mapper__.class_manager.new_instance()
    dead.infohash = "a" * 40
    e2.blacklisted_streams.append(dead)
    assert not has_gap(show, pack("Show.S01E02.1080p.mkv"))

    e2.last_state = States.Completed
    assert not has_gap(show, pack("Show.S01E02.1080p.mkv", infohash="b" * 40))


def test_movie_gap():
    movie = Movie({"title": "Film", "tmdb_id": "1"})
    movie.last_state = States.Indexed
    assert TorBoxAccountImport._has_gap(movie, pack("Film.2020.mkv"))

    movie.last_state = States.Completed
    assert not TorBoxAccountImport._has_gap(movie, pack("Film.2020.mkv"))


def test_unmatched_torrent_notifies_once(importer):
    torrent = {"id": 7, "name": "Mystery"}

    importer._unmatched(torrent, "no TMDB movie called 'Mystery'")
    importer._unmatched(torrent, "no TMDB movie called 'Mystery'")

    assert len(importer.sent) == 1
    assert "Mystery" in importer.sent[0]


class FakeTorBox:
    def __init__(self, gone=()):
        self.gone = set(gone)
        self.checked = []

    def get_torrent_info(self, torrent_id):
        self.checked.append(torrent_id)
        if torrent_id in self.gone:
            raise TorBoxNotFound(torrent_id)
        return object()


def test_many_missing_torrents_reset_nothing(importer):
    referenced = {str(i): {i} for i in range(torbox_import.MAX_REMOVED_PER_RUN + 1)}
    torbox = FakeTorBox(gone=referenced)

    assert importer._handle_removed(torbox, referenced, on_account={}) == set()
    assert torbox.checked == []
    assert importer.sent


def test_missing_from_list_but_still_on_account_is_kept(importer):
    torbox = FakeTorBox(gone=())

    assert importer._handle_removed(torbox, {"1": {10}}, on_account={}) == set()
    assert torbox.checked == ["1"]


def test_run_only_considers_finished_unused_torrents(importer, monkeypatch):
    torrents = [
        {"id": 1, "name": "Used", "download_finished": True},
        {"id": 2, "name": "Riven adding it", "download_finished": True},
        {"id": 3, "name": "Still downloading", "download_finished": False},
        {"id": 4, "name": "New", "download_finished": True},
        {"id": 5, "name": "Riven used it before", "download_finished": True},
    ]
    torbox = SimpleNamespace(list_torrents=lambda: torrents, created_ids=lambda: {"2"})
    resolved, filled = [], []

    monkeypatch.setattr(importer, "_torbox", lambda: torbox)
    monkeypatch.setattr(importer, "_referenced", lambda: {"1": {10}})
    monkeypatch.setattr(
        importer,
        "_resolve",
        lambda t: resolved.append(t["id"]) or Match("movie", "x", 40),
    )
    monkeypatch.setattr(importer, "_handle_removed", lambda *_: set())
    monkeypatch.setattr(
        importer, "_fill", lambda _tb, t, _m: filled.append(t["id"]) or 40
    )
    monkeypatch.setattr(importer, "_emit_pending", lambda: None)
    importer._state_file.write_text('{"known": ["5"]}')

    importer._run()
    importer._run()

    assert resolved == [4]  # resolved once, cached afterwards
    assert filled == [4, 4]  # gaps are re-checked every run
    assert importer._known == {"1", "2", "5"}
    assert importer._pending_events == {40}


def test_first_run_treats_existing_torrents_as_riven_s(importer, monkeypatch):
    torrents = [{"id": 1, "name": "Old leftover", "download_finished": True}]
    torbox = SimpleNamespace(list_torrents=lambda: torrents, created_ids=set)
    resolved = []

    monkeypatch.setattr(importer, "_torbox", lambda: torbox)
    monkeypatch.setattr(importer, "_referenced", dict)
    monkeypatch.setattr(importer, "_resolve", resolved.append)
    monkeypatch.setattr(importer, "_handle_removed", lambda *_: set())
    monkeypatch.setattr(importer, "_emit_pending", lambda: None)

    importer._run()

    assert resolved == []
    assert importer._state_file.exists()


def test_library_match_uses_aliases_subtitle_halves_and_season_years(
    importer, monkeypatch
):
    from datetime import datetime

    rows = [
        SimpleNamespace(
            id=301,
            title="Demon Slayer: Kimetsu no Yaiba",
            aired_at=datetime(2019, 4, 6),
            aliases={"jpn": ["kny"]},
        ),
        SimpleNamespace(
            id=486, title="Coyote vs. Acme", aired_at=datetime(2026, 8, 20), aliases={}
        ),
    ]

    class FakeSession:
        def execute(self, _query):
            return SimpleNamespace(all=lambda: rows)

    @contextmanager
    def fake_session():
        yield FakeSession()

    monkeypatch.setattr(torbox_import, "db_session", fake_session)
    lib = importer._match_library

    assert lib("show", "kny", None, exact=True).item_id == 301
    assert lib("show", "Demon Slayer", None, exact=True) is None
    assert lib("show", "Demon Slayer", None, exact=False).item_id == 301
    assert lib("show", "Kimetsu no Yaiba", 2024, exact=False).item_id == 301
    assert lib("movie", "Coyote vs Acme", 2026, exact=True).item_id == 486
    assert lib("movie", "Coyote vs Acme", 2019, exact=True) is None
