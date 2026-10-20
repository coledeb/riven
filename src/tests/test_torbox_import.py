"""TorBox account import: matching torrent names, gap checks and safety limits."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from kink import di

from program.apis.tmdb_api import TMDBApi
from program.media.item import Episode, Movie, Season, Show
from program.media.media_entry import MediaEntry
from program.media.models import ActiveStream
from program.media.state import States
from program.media.stream import Stream
from program.services.downloaders import torbox_import
from program.services.downloaders.torbox import TorBoxNotFound
from program.services.downloaders.torbox_import import Match, TorBoxAccountImport
from program.services.updaters.plex import playing_sessions


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

    titles: dict = {}
    ids: dict = {}

    def find_id(self, _kind, _source, external_id):
        return self.ids.get(external_id)

    def get_titles(self, _kind, tmdb_id):
        return self.titles.get(str(tmdb_id), set())


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
    e2.blacklist_stream(dead)
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
    assert importer.sent[0].title == "Mystery"


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


def row(id, title, aired_at=None, aliases=None, tvdb_id=None):
    return SimpleNamespace(
        id=id,
        title=title,
        aired_at=aired_at,
        aliases=aliases or {},
        tmdb_id=None,
        tvdb_id=tvdb_id,
        imdb_id=None,
    )


@pytest.fixture
def library(monkeypatch):
    rows = []

    class FakeSession:
        def execute(self, _query):
            return SimpleNamespace(all=lambda: rows)

    @contextmanager
    def fake_session():
        yield FakeSession()

    monkeypatch.setattr(torbox_import, "db_session", fake_session)
    return rows


def test_library_match_uses_aliases_subtitle_halves_and_season_years(
    importer, library
):
    from datetime import datetime

    library += [
        row(
            301,
            "Demon Slayer: Kimetsu no Yaiba",
            datetime(2019, 4, 6),
            {"jpn": ["kny"]},
        ),
        row(486, "Coyote vs. Acme", datetime(2026, 8, 20)),
    ]
    lib = importer._match_library

    assert lib("show", "kny", None, "exact").item_id == 301
    assert lib("show", "Demon Slayer", None, "exact") is None
    assert lib("show", "Demon Slayer", None, "halves").item_id == 301
    assert lib("show", "Kimetsu no Yaiba", 2024, "halves").item_id == 301
    assert lib("movie", "Coyote vs Acme", 2026, "exact").item_id == 486
    assert lib("movie", "Coyote vs Acme", 2019, "exact") is None


def test_library_two_language_name_needs_both_titles(importer, library, tmdb):
    library += [
        row(512, "Tulsa King", tvdb_id="t512"),
        row(513, "King", aliases={"cz": ["Kral"]}),
        row(514, "Law & Order", tvdb_id="t514"),
        row(515, "Law & Order: Special Victims Unit", tvdb_id="t515"),
    ]
    fake = FakeTMDB()
    fake.ids = {"t512": "153312", "t514": "549", "t515": "2734"}
    fake.titles = {"153312": {"Tulsa King", "Král Tulsy", "Król Tulsy"}}
    tmdb(fake)

    def lib(title):
        return importer._match_library("show", title, None, "two_titles")

    # The other half must be another title of the same show (TMDB or alias)
    assert lib("Král Tulsy Tulsa King").item_id == 512
    assert lib("Tulsa King Kral Tulsy").item_id == 512
    assert lib("Kral King").item_id == 513
    assert lib("Burger King") is None
    assert lib("Mistulsa King") is None
    assert lib("Law and Order SVU") is None


def test_tmdb_two_language_name_needs_both_titles(importer, tmdb):
    show = {"id": 153312, "name": "Tulsa King", "first_air_date": "2022-11-13"}
    other = {"id": 7, "name": "King", "first_air_date": "2001-01-01"}

    class SplitTMDB(FakeTMDB):
        def search(self, kind, query, year=None):
            self.searches.append((kind, query, year))
            return [r for r in (show, other) if r["name"].lower() == query]

    fake = SplitTMDB(tvdb="420")
    fake.titles = {"153312": {"Tulsa King", "Král Tulsy"}, "7": {"King"}}
    tmdb(fake)

    assert importer._match_tmdb_two_titles("show", "Král Tulsy Tulsa King", None) == (
        Match(kind="show", title="Tulsa King", tmdb_id="153312", tvdb_id="420")
    )
    assert importer._match_tmdb_two_titles("show", "Burger King", None) is None
    assert importer._match_tmdb_two_titles("show", "Tulsa King", None) is None


def with_file(item, infohash):
    item.active_stream = ActiveStream(infohash=infohash, id="1")
    item.filesystem_entries.append(MediaEntry.__mapper__.class_manager.new_instance())


def test_replace_targets_are_episodes_with_a_file_from_another_torrent():
    show = make_show()
    e1, e2 = show.seasons[0].episodes
    e1.id, e2.id = 11, 12
    with_file(e1, "b" * 40)  # from another torrent: replaced
    torrent = pack("Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv")

    targets = TorBoxAccountImport._replace_targets

    assert targets(show, torrent) == [e1]  # e2 has no file: a gap, not a replace

    with_file(e2, "a" * 40)  # already from this torrent: nothing to do
    assert targets(show, torrent) == [e1]

    dead = Stream.__mapper__.class_manager.new_instance()
    dead.infohash = "a" * 40
    e1.blacklist_stream(dead)  # failed for e1 before (e.g. a dead server)
    assert targets(show, torrent) == [e1]  # the user's add overrides that

    TorBoxAccountImport._unblacklist([e1], "a" * 40)
    assert e1.blacklisted_streams == []


def test_replace_targets_only_episodes_in_the_torrent():
    show = make_show()
    e1, e2 = show.seasons[0].episodes
    e1.id, e2.id = 11, 12
    with_file(e1, "b" * 40)
    with_file(e2, "b" * 40)

    assert TorBoxAccountImport._replace_targets(
        show, pack("Show.S01E02.1080p.mkv")
    ) == [e2]


def test_replace_targets_movie():
    movie = Movie({"title": "Film", "tmdb_id": "1"})
    assert TorBoxAccountImport._replace_targets(movie, pack("Film.2020.mkv")) == []

    with_file(movie, "b" * 40)
    assert TorBoxAccountImport._replace_targets(movie, pack("Film.2020.mkv")) == [movie]


def test_plex_sessions_matched_by_mapped_path():
    def session(file):
        part = SimpleNamespace(file=file)
        return SimpleNamespace(media=[SimpleNamespace(parts=[part])])

    playing = session(r"\\WSL-MEDIA\riven\shows\Show\Season 01\Show - s01e01.mkv")
    other = session(r"\\wsl-media\riven\shows\Show\Season 01\Show - s01e02.mkv")
    mappings = {"/mnt/debrid/riven": "\\\\wsl-media\\riven"}

    found = playing_sessions(
        [playing, other],
        ["/mnt/debrid/riven/shows/Show/Season 01/Show - s01e01.mkv"],
        mappings,
    )

    assert found == [playing]  # Windows paths compare case-insensitively
