"""Uncached TorBox downloads as a last resort (downloaders.torbox.uncached)."""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx
import pytest

from program.media.item import Movie
from program.media.stream import BlacklistReason
from program.services.downloaders import Downloader, torbox, torbox_uncached
from program.services.downloaders.torbox import TorBoxDownloader
from program.services.scrapers import seeders
from program.settings import settings_manager
from tests.test_torbox_downloader import (  # noqa: F401 - fixtures
    CACHED,
    episode_files,
    fake,
    fake_dns,
    torrent,
)

MOVIE_FILES = [{"id": 0, "name": "Movie.2020.1080p.mkv", "short_name": "Movie.2020.1080p.mkv", "size": 4_000_000_000}]

LEECH = "d" * 40
LEECH_2 = "e" * 40
FEW_SEEDS = "f" * 40


@pytest.fixture(autouse=True, scope="module")
def debrid_log_level():
    from loguru import logger

    try:
        logger.level("DEBRID")
    except ValueError:
        logger.level("DEBRID", no=25)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(torbox, "data_dir_path", tmp_path)
    monkeypatch.setattr(torbox_uncached, "data_dir_path", tmp_path)
    return tmp_path


@pytest.fixture
def settings(fake, monkeypatch):  # noqa: F811
    tb = settings_manager.settings.downloaders.torbox
    monkeypatch.setattr(tb, "active_download_slots", 5)
    monkeypatch.setattr(tb.uncached, "enabled", True)
    monkeypatch.setattr(tb.uncached, "seeder_tiers", [25, 10, 5])
    monkeypatch.setattr(tb.uncached, "stall_minutes", 60)
    monkeypatch.setattr(tb.uncached, "notify_urls", [])
    return tb.uncached


@pytest.fixture
def leeching(fake):  # noqa: F811
    """Let the fake TorBox accept uncached adds; they start at 0 %."""

    original = fake.handler
    fake.uncached_adds = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("torrents/createtorrent"):
            form = parse_qs(request.content.decode())
            infohash = form["magnet"][0].rsplit(":", 1)[-1]
            if form.get("add_only_if_cached") == ["false"]:
                fake.uncached_adds.append(infohash)
                t = torrent(fake.next_id, infohash, MOVIE_FILES, finished=False)
                t["progress"] = 0
                t["active"] = True
                fake.torrents[fake.next_id] = t
                fake.next_id += 1
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "detail": "ok",
                        "data": {"torrent_id": t["id"], "hash": infohash},
                    },
                )
        return original(request)

    fake.handler = handler
    return fake


@pytest.fixture
def tb(fake, settings, leeching, monkeypatch):  # noqa: F811, ARG001
    fake.cached[CACHED] = MOVIE_FILES
    downloader = TorBoxDownloader()
    assert downloader.initialized
    # No database in these tests: nothing links to any torrent
    monkeypatch.setattr(downloader, "_torrent_in_use", lambda _tid: False)
    return downloader


def stream(infohash, seeds, resolution="1080p", rank=100, title=None):
    return SimpleNamespace(
        infohash=infohash,
        seeders=seeds,
        resolution=resolution,
        rank=rank,
        raw_title=title or f"Movie.2020.{resolution}-{infohash[:4]}",
    )


def movie(streams, item_id=1):
    item = Mock(spec=Movie)
    item.id = item_id
    item.type = "movie"
    item.log_string = "Movie (2020)"
    item.parent = None
    item.streams = list(streams)
    blacklisted = []
    item.blacklisted = blacklisted

    def blacklist_stream(s, reason):
        item.streams.remove(s)
        blacklisted.append((s.infohash, reason))
        return True

    item.blacklist_stream = Mock(side_effect=blacklist_stream)
    return item


def make_downloader(tb):
    downloader = Downloader.__new__(Downloader)
    downloader.initialized = True
    downloader.services = {TorBoxDownloader: tb}
    downloader.initialized_services = [tb]
    downloader.service = tb
    downloader._service_cooldowns = {}
    downloader.subtitles_enabled = False
    return downloader


def run(downloader, item):
    """First result only, like the event manager."""

    return next(downloader.run(item))


# --- seeders from Torrentio ---


def test_parse_count_reads_torrentio_stats_line():
    assert seeders.parse_count(" 12 💾 1.4 GB ⚙️ ThePirateBay") == 12
    assert seeders.parse_count(" 💾 1.4 GB") is None


def test_seeder_counts_are_remembered_per_hash():
    seeders.record("ABC" * 13 + "D", 7)
    assert seeders.get(("abc" * 13 + "d")) == 7
    assert seeders.get("0" * 40) is None


# --- choosing a candidate ---


def test_candidates_prefer_seeder_tier_then_quality(tb):
    uhd_few = stream("1" * 40, 12, "2160p")
    hd_many = stream("2" * 40, 30, "1080p")
    hd_ok = stream("3" * 40, 26, "720p")
    too_few = stream("4" * 40, 4, "2160p")
    unknown = stream("5" * 40, None, "2160p")
    ordered = [too_few, unknown, uhd_few, hd_many, hd_ok]  # quality order
    item = movie(ordered)
    for s in ordered:
        tb.uncached.remember_not_cached(item.id, s.infohash)

    assert tb.uncached.candidates(item, ordered) == [hd_many, hd_ok, uhd_few]


# --- downloader flow ---


def test_cached_stream_wins_over_better_seeded_uncached(tb, leeching):
    cached = stream(CACHED, None, "720p")
    uncached = stream(LEECH, 100, "2160p")
    item = movie([uncached, cached])
    downloader = make_downloader(tb)
    downloader.update_item_attributes = Mock(return_value=True)

    result = run(downloader, item)

    assert result.run_at is None
    assert leeching.uncached_adds == []
    assert item.blacklisted == []  # the uncached one is kept for later


def test_uncached_download_starts_as_last_resort_and_notifies(tb, leeching, settings, monkeypatch):
    sent = []
    monkeypatch.setattr(tb.uncached, "_notify", lambda title, msg: sent.append(msg))
    few = stream(FEW_SEEDS, 3)
    good = stream(LEECH, 40)
    item = movie([few, good])
    downloader = make_downloader(tb)

    result = run(downloader, item)

    assert leeching.uncached_adds == [LEECH]
    assert result.run_at is not None  # waits for the download
    assert item.blacklisted == [(FEW_SEEDS, BlacklistReason.NOT_CACHED)]
    assert "Seeders reported: 40" in sent[0]
    assert "download slot 1/5" in sent[0]
    saved = json.loads((torbox_uncached.data_dir_path / torbox_uncached.STATE_FILE).read_text())
    assert saved["1"]["infohash"] == LEECH and saved["1"]["seeders"] == 40


def test_waits_without_new_checks_while_downloading(tb, leeching):
    item = movie([stream(LEECH, 40)])
    downloader = make_downloader(tb)
    run(downloader, item)
    leeching.calls.clear()

    result = run(downloader, item)

    assert result.run_at is not None
    assert leeching.count("torrents/checkcached") == 0
    assert leeching.uncached_adds == [LEECH]


def test_finished_download_is_used(tb, leeching):
    item = movie([stream(LEECH, 40)])
    downloader = make_downloader(tb)
    run(downloader, item)
    t = next(t for t in leeching.torrents.values() if t["hash"] == LEECH)
    t.update(progress=1, download_finished=True, download_state="cached")
    downloader.update_item_attributes = Mock(return_value=True)

    result = run(downloader, item)

    assert result.run_at is None
    downloader.update_item_attributes.assert_called_once()
    assert tb.uncached.torrent_ids() == set()


def test_stalled_download_is_dropped_and_next_candidate_started(tb, leeching, settings, monkeypatch):
    sent = []
    monkeypatch.setattr(tb.uncached, "_notify", lambda title, msg: sent.append(title))
    item = movie([stream(LEECH, 40), stream(LEECH_2, 12)])
    downloader = make_downloader(tb)
    run(downloader, item)
    first_id = next(t["id"] for t in leeching.torrents.values() if t["hash"] == LEECH)
    monkeypatch.setattr(
        torbox_uncached.time, "time", lambda: time.time_ns() / 1e9 + 61 * 60
    )

    result = run(downloader, item)

    assert first_id not in leeching.torrents  # deleted
    assert item.blacklisted == [(LEECH, BlacklistReason.DOWNLOAD_STALLED)]
    assert leeching.uncached_adds == [LEECH, LEECH_2]
    assert result.run_at is not None
    assert sent == [
        "Riven: uncached download started",
        "Riven: uncached download given up",
        "Riven: uncached download started",
    ]


def test_no_free_slot_waits_and_starts_nothing(tb, leeching, settings):
    for i in range(5):
        t = torrent(500 + i, f"{i}" * 40, episode_files(1), finished=False)
        t["active"] = True
        leeching.torrents[500 + i] = t
    item = movie([stream(LEECH, 40)])

    result = run(make_downloader(tb), item)

    assert leeching.uncached_adds == []
    assert result.run_at is not None
    assert item.blacklisted == []


def test_disabled_keeps_cached_only_behaviour(tb, leeching, settings, monkeypatch):
    monkeypatch.setattr(settings, "enabled", False)
    item = movie([stream(LEECH, 40)])

    result = run(make_downloader(tb), item)

    assert leeching.uncached_adds == []
    assert result.run_at is None
    assert item.blacklisted == [(LEECH, BlacklistReason.NOT_CACHED)]


def test_related_item_waits_for_running_download(tb, leeching):
    season = SimpleNamespace(id=10, parent=SimpleNamespace(id=9, parent=None))
    item = movie([stream(LEECH, 40)], item_id=10)
    item.parent = season.parent
    downloader = make_downloader(tb)
    run(downloader, item)
    episode = movie([stream(LEECH_2, 40)], item_id=11)
    episode.parent = season
    leeching.calls.clear()

    result = run(downloader, episode)

    assert result.run_at is not None
    assert leeching.count("torrents/checkcached") == 0


def test_running_download_is_never_removed_as_unused(tb, leeching):
    run(make_downloader(tb), movie([stream(LEECH, 40)]))
    tid = next(str(t["id"]) for t in leeching.torrents.values() if t["hash"] == LEECH)

    assert tid not in tb._due_unused({tid}, used=set())
    assert tid not in tb._unused_since
