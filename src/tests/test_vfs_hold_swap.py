"""A re-sourced file stays visible and is swapped in place (no gap for media servers)."""

import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pyfuse3
import pytest
import trio
import trio.testing

import program.media.media_entry as media_entry_module
import program.services.filesystem.vfs.rivenvfs as rivenvfs_module
from program.services.filesystem.vfs.vfs_node import VFSFile, VFSRoot


class FakeEntry:
    def __init__(self, original_filename, file_size, path):
        self.original_filename = original_filename
        self.file_size = file_size
        self.created_at = datetime(2026, 10, 18, 10, 0)
        self.updated_at = datetime(2026, 10, 18, 10, 0)
        self.available_in_vfs = False
        self._path = path

    def get_all_vfs_paths(self):
        return [self._path]


class FakeItem:
    def __init__(self, item_id, entry):
        self.id = item_id
        self.media_entry = entry
        self.subtitles = []


@pytest.fixture
def vfs(monkeypatch):
    monkeypatch.setattr(pyfuse3, "invalidate_inode", lambda *_a, **_k: None)
    monkeypatch.setattr(pyfuse3, "invalidate_entry_async", lambda *_a, **_k: None)
    # rivenvfs checks isinstance(entry, MediaEntry), importing it at call time
    monkeypatch.setattr(media_entry_module, "MediaEntry", FakeEntry)

    vfs = object.__new__(rivenvfs_module.RivenVFS)
    vfs._tree_lock = threading.RLock()
    vfs._root = VFSRoot()
    vfs._inode_to_node = {pyfuse3.ROOT_INODE: vfs._root}
    vfs._next_inode = pyfuse3.InodeT(pyfuse3.ROOT_INODE + 1)
    vfs._pending_invalidations = set()
    vfs._held = {}
    vfs._read_errors = {}
    vfs._replaced_ids = set()
    vfs._hold_timeout = 600
    vfs._hold_stop_sent = set()
    vfs._last_read = {}
    vfs._file_handles = {}
    vfs._plex_stop_skip_until = 0.0
    vfs.stops = []
    vfs._stop_plex_playback = lambda paths, message, wait=0: vfs.stops.append(
        (paths, message)
    )
    return vfs


MOVIE = "/movies/Shawshank (1994)/Shawshank (1994).mkv"


def _file(vfs, path):
    node = vfs._get_node_by_path(path)
    return node if isinstance(node, VFSFile) else None


def test_held_file_is_swapped_in_place(vfs):
    item = FakeItem(231, FakeEntry("Grym.mkv", 100, MOVIE))
    assert vfs.add(item)
    inode = _file(vfs, MOVIE).inode

    vfs.hold(item)
    assert _file(vfs, MOVIE) is not None  # still visible during re-source
    assert vfs._is_held("Grym.mkv")

    item.media_entry = FakeEntry("DDR.mkv", 200, MOVIE)
    assert vfs.add(item)

    node = _file(vfs, MOVIE)
    assert node.inode == inode
    assert (node.original_filename, node.file_size) == ("DDR.mkv", 200)
    assert not vfs._is_held("Grym.mkv")
    assert vfs.pop_replaced(231) is True
    assert vfs.pop_replaced(231) is False


def test_held_path_dropped_when_new_file_has_other_path(vfs):
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs.hold(item)

    new_path = MOVIE.replace(".mkv", ".mp4")
    item.media_entry = FakeEntry("b.mp4", 200, new_path)
    vfs.add(item)

    assert _file(vfs, MOVIE) is None
    assert _file(vfs, new_path).original_filename == "b.mp4"


def test_remove_drops_held_file(vfs):
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs.hold(item)

    item.media_entry = None
    item.filesystem_entry = None
    vfs.remove(item)

    assert _file(vfs, MOVIE) is None
    assert vfs._held == {}


def test_open_waits_for_swap(vfs):
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs.hold(item)
    inode = _file(vfs, MOVIE).inode

    async def main():
        async with trio.open_nursery() as nursery:

            async def swap():
                await trio.sleep(0.2)
                item.media_entry = FakeEntry("b.mkv", 200, MOVIE)
                vfs.add(item)

            nursery.start_soon(swap)
            await vfs._await_swap(inode, "a.mkv")

    trio.run(main)
    assert _file(vfs, MOVIE).original_filename == "b.mkv"


def test_hold_expires_without_new_source(vfs):
    vfs._hold_timeout = 0.1
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs.hold(item)

    time.sleep(0.3)

    assert _file(vfs, MOVIE) is None
    assert vfs._held == {}

    # A source found later is added as a new file
    item.media_entry = FakeEntry("b.mkv", 200, MOVIE)
    assert vfs.add(item)
    assert _file(vfs, MOVIE).original_filename == "b.mkv"
    assert vfs.pop_replaced(1) is False


def test_expiry_ignores_a_newer_hold(vfs):
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs.hold(item)
    item.media_entry = FakeEntry("b.mkv", 200, MOVIE)
    vfs.add(item)
    vfs.hold(item)  # the new source died too

    vfs._expire_hold(1, "a.mkv")  # timer from the first hold

    assert _file(vfs, MOVIE) is not None
    assert vfs._is_held("b.mkv")


def test_failed_read_waits_for_swap_and_retries(vfs):
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    inode = _file(vfs, MOVIE).inode
    vfs._file_handles = {5: {"inode": inode}}
    vfs._active_streams = {}
    vfs._active_streams_lock = trio.Lock()

    class Stream:
        def __init__(self, name):
            self.name = name
            self.closed = False

        async def read(self, **_k):
            if self.name == "a.mkv":
                # The dead link resets the item (holding its file), then fails
                vfs.hold(item)
                raise RuntimeError("dead link")
            return b"new"

        async def close(self):
            self.closed = True

        def build_log_message(self, msg):
            return msg

    streams = []

    async def get_stream(path, fh, file_size, original_filename):  # noqa: ARG001
        key = vfs._stream_key(path, fh)
        if key not in vfs._active_streams:
            vfs._active_streams[key] = Stream(original_filename)
            streams.append(vfs._active_streams[key])
        return vfs._active_streams[key]

    vfs._get_stream = get_stream

    async def main():
        async with trio.open_nursery() as nursery:

            async def swap():
                await trio.sleep(0.2)
                item.media_entry = FakeEntry("b.mkv", 200, MOVIE)
                vfs.add(item)

            nursery.start_soon(swap)
            assert await vfs.read(5, 0, 3) == b"new"

    trio.run(main)
    assert streams[0].closed


def test_playback_is_stopped_at_the_swap_with_reads_still_held(vfs):
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs._file_handles[3] = {"inode": 1, "read": "a.mkv"}
    vfs.hold(item)
    assert vfs.stops == []  # nothing stopped while the search runs
    seen = []

    def stop(paths, message, wait=0):
        node = _file(vfs, MOVIE)
        seen.append((paths, message, wait > 0, vfs._is_held("a.mkv"), node.original_filename))

    vfs._stop_plex_playback = stop
    item.media_entry = FakeEntry("b.mkv", 200, MOVIE)
    vfs.add(item)

    assert seen == [
        ([MOVIE], rivenvfs_module.SWAP_STOP_MESSAGE, True, True, "a.mkv")
    ]
    assert _file(vfs, MOVIE).original_filename == "b.mkv"


def test_adding_a_file_that_was_not_held_stops_nothing(vfs):
    vfs.add(FakeItem(1, FakeEntry("a.mkv", 100, MOVIE)))

    assert vfs.stops == []


def test_pressing_play_on_a_dead_link_just_starts_on_the_new_file(vfs):
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs._file_handles[3] = {"inode": 1}  # open, but nothing served yet
    vfs.hold(item)

    item.media_entry = FakeEntry("b.mkv", 200, MOVIE)
    vfs.add(item)

    assert vfs.stops == []


def test_reads_count_as_playback_for_a_while_without_a_handle(vfs):
    for item_id, name, age, stopped in (
        (1, "a.mkv", 10, True),
        (2, "c.mkv", rivenvfs_module.RECENTLY_READ_SECONDS + 1, False),
    ):
        vfs.stops.clear()
        path = f"/movies/{name}"
        item = FakeItem(item_id, FakeEntry(name, 100, path))
        vfs.add(item)
        vfs._last_read[name] = time.monotonic() - age
        vfs.hold(item)
        item.media_entry = FakeEntry(f"new-{name}", 200, path)
        vfs.add(item)
        assert bool(vfs.stops) is stopped
        assert name not in vfs._last_read


def test_swap_does_not_stop_someone_pressing_play_after_an_earlier_stop(vfs):
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs._file_handles[3] = {"inode": 1, "read": "a.mkv"}  # the old reader, closing
    vfs.playback_stopped("a.mkv")
    vfs.hold(item)

    item.media_entry = FakeEntry("b.mkv", 200, MOVIE)
    vfs.add(item)

    assert vfs.stops == []
    assert vfs._hold_stop_sent == set()  # a later re-source can stop again


def test_a_new_copy_with_another_path_still_stops_the_old_path(vfs):
    item = FakeItem(1, FakeEntry("a.mp4", 100, "/movies/M/M.mp4"))
    vfs.add(item)
    vfs._last_read["a.mp4"] = time.monotonic()
    vfs.hold(item)

    item.media_entry = FakeEntry("b.mkv", 200, "/movies/M/M.mkv")
    vfs.add(item)

    assert vfs.stops[0][0] == ["/movies/M/M.mp4"]


def test_a_read_that_gives_up_waiting_stops_playback_once(vfs):
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs.hold(item)
    inode = _file(vfs, MOVIE).inode

    async def main():
        for _ in range(2):
            with pytest.raises(pyfuse3.FUSEError):
                await vfs._await_swap(inode, "a.mkv")

    trio.run(main, clock=trio.testing.MockClock(autojump_threshold=0))

    assert vfs.stops == [([MOVIE], rivenvfs_module.SOURCE_LOST_STOP_MESSAGE)]

    # The stop already happened: whoever presses play now gets the new copy
    item.media_entry = FakeEntry("b.mkv", 200, MOVIE)
    vfs._last_read["a.mkv"] = time.monotonic()
    vfs.add(item)
    assert len(vfs.stops) == 1


def test_expired_hold_stops_playback(vfs):
    vfs._hold_timeout = 0.05
    item = FakeItem(1, FakeEntry("a.mkv", 100, MOVIE))
    vfs.add(item)
    vfs.hold(item)

    time.sleep(0.2)

    assert vfs.stops == [([MOVIE], rivenvfs_module.SOURCE_LOST_STOP_MESSAGE)]


def _plex_services(monkeypatch, plex):
    from program.program import Program
    from program.services.updaters.plex import PlexUpdater

    services = SimpleNamespace(
        updater=SimpleNamespace(library_path="/mnt/debrid/riven", services={PlexUpdater: plex})
    )
    monkeypatch.setattr(
        rivenvfs_module, "di", {Program: SimpleNamespace(services=services)}
    )


def test_stop_finds_sessions_by_library_path(monkeypatch):
    stopped = []
    asked = []

    def sessions_playing(paths):
        asked.append(paths)
        return [
            SimpleNamespace(stop=lambda reason: stopped.append(reason)),
            SimpleNamespace(stop=lambda reason: (_ for _ in ()).throw(OSError("no"))),
            SimpleNamespace(stop=lambda reason: stopped.append(reason)),
        ]

    _plex_services(
        monkeypatch, SimpleNamespace(initialized=True, sessions_playing=sessions_playing)
    )

    rivenvfs_module.RivenVFS._stop_plex_sessions([MOVIE], "msg")

    assert asked == [["/mnt/debrid/riven" + MOVIE]]
    assert stopped == ["msg", "msg"]  # one failure doesn't spare the others


def test_stop_problems_never_reach_the_swap(monkeypatch):
    def broken(_paths):
        raise OSError("Plex is down")

    _plex_services(
        monkeypatch, SimpleNamespace(initialized=True, sessions_playing=broken)
    )

    rivenvfs_module.RivenVFS._stop_plex_sessions([MOVIE], "msg")


def test_a_plex_that_hangs_delays_one_swap_then_is_skipped(monkeypatch):
    release = threading.Event()
    asked = []

    def hang(_paths):
        asked.append(1)
        release.wait(5)
        return []

    _plex_services(monkeypatch, SimpleNamespace(initialized=True, sessions_playing=hang))
    vfs = object.__new__(rivenvfs_module.RivenVFS)
    vfs._plex_stop_skip_until = 0.0

    started = time.monotonic()
    vfs._stop_plex_playback([MOVIE], "msg", wait=0.1)
    vfs._stop_plex_playback([MOVIE], "msg", wait=0.1)
    release.set()

    assert time.monotonic() - started < 1
    assert asked == [1]
