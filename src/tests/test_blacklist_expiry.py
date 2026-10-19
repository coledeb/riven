"""Blacklist entries for temporary problems expire back into the item's pool."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from program.media.item import Movie
from program.media.stream import BlacklistReason, Stream
from program.services.streaming.exceptions import DebridServiceLinkUnavailable
from program.settings import settings_manager

NOW = datetime(2026, 10, 19, 12, 0)


def make_stream(infohash="a" * 40):
    torrent = SimpleNamespace(
        raw_title="Movie 2020 1080p",
        infohash=infohash,
        data=SimpleNamespace(parsed_title="Movie", resolution="1080p"),
        rank=100,
        lev_ratio=1.0,
    )
    return Stream(torrent)


@pytest.fixture
def config(monkeypatch):
    config = settings_manager.settings.scraping.blacklist
    for name, value in {
        "expiry_enabled": True,
        "cdn_host_down_hours": 24,
        "slow_source_hours": 24,
        "not_cached_hours": 72,
        "torrent_removed_hours": 24,
        "download_error_hours": 72,
        "max_strikes": 4,
    }.items():
        monkeypatch.setattr(config, name, value)
    return config


def expiry(movie, stream):
    entry = next(e for e in movie.blacklist_entries if e.stream == stream)
    return entry.expires_at - entry.blacklisted_at if entry.expires_at else None


@pytest.mark.parametrize(
    "reason, hours",
    [
        (BlacklistReason.CDN_HOST_DOWN, 24),
        (BlacklistReason.SLOW_SOURCE, 24),
        (BlacklistReason.NOT_CACHED, 72),
        (BlacklistReason.TORRENT_REMOVED, 24),
        (BlacklistReason.DOWNLOAD_ERROR, 72),
        (BlacklistReason.WRONG_FILES, None),
        (BlacklistReason.MANUAL, None),
        (BlacklistReason.UNKNOWN, None),
    ],
)
def test_expiry_depends_on_reason(config, reason, hours):
    movie, stream = Movie({}), make_stream()
    movie.streams.append(stream)

    assert movie.blacklist_stream(stream, reason)
    assert stream not in movie.streams
    assert stream in movie.blacklisted_streams
    assert movie.blacklist_entries[0].reason == reason
    assert expiry(movie, stream) == (timedelta(hours=hours) if hours else None)


def test_zero_hours_or_expiry_off_means_permanent(config):
    config.slow_source_hours = 0
    movie, stream = Movie({}), make_stream()
    movie.blacklist_stream(stream, BlacklistReason.SLOW_SOURCE)
    assert expiry(movie, stream) is None

    config.slow_source_hours = 24
    config.expiry_enabled = False
    movie, stream = Movie({}), make_stream()
    movie.blacklist_stream(stream, BlacklistReason.SLOW_SOURCE)
    assert expiry(movie, stream) is None


def test_expired_entry_goes_back_into_the_pool(config):
    movie, stream = Movie({}), make_stream()
    movie.blacklist_stream(stream, BlacklistReason.CDN_HOST_DOWN)
    entry = movie.blacklist_entries[0]

    assert movie.release_expired_blacklist(entry.expires_at - timedelta(minutes=1)) == []
    assert stream in movie.blacklisted_streams

    assert movie.release_expired_blacklist(entry.expires_at) == [stream]
    assert stream in movie.streams
    assert stream not in movie.blacklisted_streams
    assert movie.blacklist_entries == [entry]  # kept to count strikes
    assert movie.release_expired_blacklist(entry.expires_at) == []


def test_permanent_entries_are_never_released(config):
    movie, stream = Movie({}), make_stream()
    movie.blacklist_stream(stream, BlacklistReason.MANUAL)

    assert movie.release_expired_blacklist(NOW + timedelta(days=3650)) == []
    assert stream in movie.blacklisted_streams


def test_strikes_double_the_time_then_become_permanent(config):
    movie, stream = Movie({}), make_stream()
    durations = []

    for _ in range(5):
        assert movie.blacklist_stream(stream, BlacklistReason.SLOW_SOURCE)
        durations.append(expiry(movie, stream))
        entry = movie.blacklist_entries[0]
        if entry.expires_at:
            movie.release_expired_blacklist(entry.expires_at)

    assert len(movie.blacklist_entries) == 1
    assert movie.blacklist_entries[0].strikes == 5
    assert durations == [timedelta(hours=h) for h in (24, 48, 96, 192)] + [None]


def test_strike_takes_the_new_reason(config):
    movie, stream = Movie({}), make_stream()
    movie.blacklist_stream(stream, BlacklistReason.CDN_HOST_DOWN)
    movie.release_expired_blacklist(movie.blacklist_entries[0].expires_at)

    movie.blacklist_stream(stream, BlacklistReason.NOT_CACHED)
    entry = movie.blacklist_entries[0]
    assert entry.reason == BlacklistReason.NOT_CACHED
    assert entry.strikes == 2
    assert expiry(movie, stream) == timedelta(hours=144)


def test_unblacklist_forgets_strikes(config):
    movie, stream = Movie({}), make_stream()
    movie.blacklist_stream(stream, BlacklistReason.SLOW_SOURCE)
    movie.unblacklist_stream(stream)

    assert movie.blacklist_entries == []
    assert stream in movie.streams

    movie.blacklist_stream(stream, BlacklistReason.SLOW_SOURCE)
    assert movie.blacklist_entries[0].strikes == 1


def test_clear_blacklist(config):
    movie = Movie({})
    movie.blacklist_stream(make_stream("a" * 40), BlacklistReason.MANUAL)
    movie.blacklist_stream(make_stream("b" * 40), BlacklistReason.SLOW_SOURCE)

    movie.clear_blacklist()
    assert movie.blacklisted_streams == []


def test_dead_link_reason():
    assert DebridServiceLinkUnavailable("torbox", "x").reason == "torrent_removed"
    e = DebridServiceLinkUnavailable("torbox", "x", reason="cdn_host_down")
    assert BlacklistReason(e.reason) is BlacklistReason.CDN_HOST_DOWN
