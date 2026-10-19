"""Slow-stream replace: what counts as slow, fresh link first, cooldown, outages."""

from types import SimpleNamespace

import pytest

from program.services.streaming.media_stream import MediaStream
from program.services.streaming.slow_streams import (
    FileStats,
    SlowFile,
    SlowStreamMonitor,
    StreamHealth,
    assess,
    session_bitrate,
)
from program.settings.models import SlowStreamModel

MB = 1024 * 1024
BITRATE = 8_000_000  # 1 MB/s


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def stats_with(samples=(), stalls=()):
    return FileStats(
        path="/shows/X/x.mkv",
        file_size=1000 * MB,
        url="https://cdn1.example/x",
        samples=list(samples),
        stalls=list(stalls),
    )


def test_two_timed_out_reads_are_slow():
    settings = SlowStreamModel()
    stats = stats_with(stalls=[900.0, 990.0])

    assert assess(stats, 1000.0, None, settings) == "2 reads timed out in 300 s"


def test_old_timed_out_reads_dont_count():
    settings = SlowStreamModel()
    stats = stats_with(stalls=[600.0, 990.0])

    assert assess(stats, 1000.0, BITRATE, settings) is None


def test_downloading_slower_than_playback_is_slow():
    settings = SlowStreamModel()
    # 60 chunks of 1 MB, each took 1.5 s: about 5.6 Mbps for an 8 Mbps file
    stats = stats_with(samples=[(940.0 + i, MB, 1.5) for i in range(60)])

    reason = assess(stats, 1000.0, BITRATE, settings)

    assert reason and reason.startswith("downloading at 5.6 Mbps")


def test_fast_enough_or_too_little_data_is_not_slow():
    settings = SlowStreamModel()
    fast = stats_with(samples=[(940.0 + i, MB, 0.5) for i in range(60)])
    # Slow chunks, but only 10 s spent downloading: Plex isn't waiting on it
    brief = stats_with(samples=[(990.0 + i, MB, 1.0) for i in range(10)])

    assert assess(fast, 1000.0, BITRATE, settings) is None
    assert assess(brief, 1000.0, BITRATE, settings) is None


def test_bitrate_from_plex_duration():
    session = SimpleNamespace(duration=1000 * 1000, media=[])
    from_media = SimpleNamespace(
        duration=None, media=[SimpleNamespace(duration=2000 * 1000)]
    )

    assert session_bitrate(1000 * MB, [session]) == pytest.approx(MB * 8)
    assert session_bitrate(1000 * MB, [from_media]) == pytest.approx(MB * 4)
    assert session_bitrate(1000 * MB, [SimpleNamespace(media=[])]) is None


def test_health_forgets_idle_files_and_their_links():
    clock = Clock()
    health = StreamHealth(clock=clock)
    health.record_stall("a.mkv", path="/a.mkv", file_size=1, url="u")
    health.push_url("a.mkv", "fresh")

    clock.now += 601

    assert health.snapshot() == {}
    assert health.pushed_url("a.mkv") is None


def test_stream_switches_to_a_pushed_link_once(monkeypatch):
    from program.services.streaming import media_stream

    health = StreamHealth()
    monkeypatch.setattr(media_stream, "stream_health", health)
    stream = SimpleNamespace(
        file_metadata=SimpleNamespace(original_filename="a.mkv"),
        target_url=SimpleNamespace(value="old"),
        _pushed_url=None,
        build_log_message=lambda m: m,
    )

    health.push_url("a.mkv", "fresh")
    MediaStream._switch_to_pushed_url(stream)
    assert stream.target_url.value == "fresh"

    # The stream refreshed the link itself later: don't go back to the pushed one
    stream.target_url.value = "newer"
    MediaStream._switch_to_pushed_url(stream)
    assert stream.target_url.value == "newer"


class FakePlex:
    def __init__(self, playing):
        self.playing = playing

    def sessions_playing(self, paths):
        return (
            [SimpleNamespace(duration=1000 * 1000, media=[])]
            if any(p in self.playing for p in paths)
            else []
        )


@pytest.fixture
def monitor(monkeypatch):
    clock = Clock()
    health = StreamHealth(clock=clock)
    monitor = SlowStreamMonitor(health=health, clock=clock)
    monitor.clock = clock
    monitor.plex = FakePlex(set())
    monitor.calls = []
    monitor.fresh = True
    infohashes = {}
    monitor.infohashes = infohashes

    monkeypatch.setattr(monitor, "_plex", lambda: monitor.plex)
    monkeypatch.setattr(monitor, "_library_path", lambda p: p)
    monkeypatch.setattr(
        monitor,
        "_describe",
        lambda key, stats, sessions, reason: SlowFile(
            key, stats, sessions, reason, hash(key) % 1000, key, infohashes.get(key)
        ),
    )
    monkeypatch.setattr(
        monitor,
        "_fresh_link",
        lambda f: monitor.calls.append(("fresh", f.key)) or monitor.fresh,
    )
    monkeypatch.setattr(
        monitor, "_replace", lambda f: monitor.calls.append(("replace", f.key))
    )
    monkeypatch.setattr(
        monitor, "_notify", lambda m: monitor.calls.append(("notify", m))
    )
    return monitor


def stall_twice(monitor, key, url="https://cdn1.example/x"):
    for _ in range(2):
        monitor.health.record_stall(key, path=f"/{key}", file_size=1000 * MB, url=url)


def test_slow_file_gets_a_fresh_link_then_a_replace_then_a_cooldown(monitor):
    monitor.plex.playing = {"/a.mkv"}

    stall_twice(monitor, "a.mkv")
    monitor.check()
    assert monitor.calls == [("fresh", "a.mkv")]

    stall_twice(monitor, "a.mkv")
    monitor.check()
    assert monitor.calls[-1] == ("replace", "a.mkv")

    monitor.clock.now += 3600
    stall_twice(monitor, "a.mkv")
    monitor.check()
    assert monitor.calls.count(("replace", "a.mkv")) == 1


def test_no_other_link_replaces_right_away(monitor):
    monitor.plex.playing = {"/a.mkv"}
    monitor.fresh = False

    stall_twice(monitor, "a.mkv")
    monitor.check()

    assert monitor.calls == [("fresh", "a.mkv"), ("replace", "a.mkv")]


def test_files_not_playing_in_plex_are_ignored(monitor):
    stall_twice(monitor, "a.mkv")
    monitor.check()

    assert monitor.calls == []


def test_several_torrents_slow_at_once_only_notifies(monitor):
    monitor.plex.playing = {"/a.mkv", "/b.mkv"}
    monitor.infohashes.update({"a.mkv": "aaa", "b.mkv": "bbb"})

    stall_twice(monitor, "a.mkv")
    stall_twice(monitor, "b.mkv")
    monitor.check()

    assert [c[0] for c in monitor.calls] == ["notify"]
    assert "connection or debrid service problem" in monitor.calls[0][1]

    # Still slow a minute later: no second message
    monitor.clock.now += 60
    stall_twice(monitor, "a.mkv")
    stall_twice(monitor, "b.mkv")
    monitor.check()

    assert [c[0] for c in monitor.calls] == ["notify"]


def test_two_episodes_of_one_pack_are_not_an_outage(monitor):
    monitor.plex.playing = {"/a.mkv", "/b.mkv"}
    monitor.infohashes.update({"a.mkv": "pack", "b.mkv": "pack"})

    stall_twice(monitor, "a.mkv")
    stall_twice(monitor, "b.mkv")
    monitor.check()

    assert sorted(monitor.calls) == [("fresh", "a.mkv"), ("fresh", "b.mkv")]


def test_still_slow_on_another_server_only_notifies(monitor):
    monitor.plex.playing = {"/a.mkv"}

    stall_twice(monitor, "a.mkv")
    monitor.check()
    assert monitor.calls == [("fresh", "a.mkv")]

    # The fresh link moved the stream to another server, and it's slow there too
    stall_twice(monitor, "a.mkv", url="https://cdn2.example/x")
    monitor.check()

    assert [c[0] for c in monitor.calls] == ["fresh", "notify"]
    assert "two different servers" in monitor.calls[1][1]
