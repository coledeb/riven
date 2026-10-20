"""Overlapping reads on one file handle must all be served.

Samba (and so Plex on Windows) keeps several reads in flight on one open file.
Each read must get its chunks fetched, even when a newer read arrives while the
stream is still busy with an older one.
"""

import tempfile
from pathlib import Path

import httpx
import trio
from kink import di

from program.services.streaming import media_stream
from program.services.streaming.cache import Cache, CacheConfig
from program.services.streaming.chunker import ChunkCacheNotifier, Chunker
from program.services.streaming.config import Config
from program.services.streaming.exceptions import ChunksTooSlowException
from program.services.streaming.file_metadata import FileMetadata
from program.services.streaming.media_stream import MediaStream
from program.services.streaming.recent_reads import RecentReads
from program.services.streaming.session_statistics import SessionStatistics

MB = 1024 * 1024
FILE_SIZE = 64 * MB
DATA = bytes(i % 251 for i in range(FILE_SIZE))
URL = "https://cdn.example/dld/file?token=t"


class SlowBody(httpx.AsyncByteStream):
    """Serves the range in 256 KB pieces with a little latency, like a CDN."""

    stall_at: int | None = None  # byte offset where the CDN fails once
    stall_error: type[Exception] = httpx.ReadTimeout

    def __init__(self, start: int, end: int):
        self.start, self.end = start, end

    async def __aiter__(self):
        pos = self.start
        while pos <= self.end:
            await trio.sleep(0.01)
            if SlowBody.stall_at is not None and pos >= SlowBody.stall_at:
                SlowBody.stall_at = None
                raise SlowBody.stall_error("stalled")
            piece = DATA[pos : min(pos + 256 * 1024, self.end + 1)]
            pos += len(piece)
            yield piece


def handler(request: httpx.Request) -> httpx.Response:
    spec = request.headers["Range"].removeprefix("bytes=")
    start_s, _, end_s = spec.partition("-")
    start = int(start_s)
    end = int(end_s) if end_s else FILE_SIZE - 1
    return httpx.Response(
        206,
        headers={
            "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{FILE_SIZE}",
        },
        stream=SlowBody(start, end),
    )


def make_stream(nursery) -> MediaStream:
    stream = MediaStream.__new__(MediaStream)
    stream.fh = 1
    stream.nursery = nursery
    stream.provider = "torbox"
    stream.recent_reads = RecentReads()
    stream._waiting_reads = []
    stream._waiting_reads_changed = media_stream.trio_util.AsyncValue(0)
    stream.is_streaming = media_stream.trio_util.AsyncBool(False)
    stream.is_killed = media_stream.trio_util.AsyncBool(False)
    stream._stream_error = media_stream.trio_util.AsyncValue(None)
    stream.enable_tracing = False
    stream.target_url = media_stream.trio_util.AsyncValue(URL)
    stream._pushed_url = None
    stream.config = Config(
        chunk_size=1 * MB,
        activity_timeout_seconds=60,
        chunk_wait_timeout_seconds=5,
        connect_timeout_seconds=5,
    )
    stream.session_statistics = SessionStatistics()
    stream.file_metadata = FileMetadata(
        file_size=FILE_SIZE, path="/movies/a.mkv", original_filename="a.mkv"
    )
    stream.chunker = Chunker(
        cache_key="a.mkv",
        chunk_size=stream.config.chunk_size,
        header_size=stream.config.header_size,
        footer_size=stream.footer_size,
        file_size=FILE_SIZE,
    )
    stream.async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream.build_log_message = lambda message: message
    return stream


async def play(
    in_flight: int, read_size: int, total: int, jumps: tuple[int, ...] = ()
) -> list[Exception]:
    """Read `total` bytes from each start position with `in_flight` reads outstanding, like Samba."""

    errors: list[Exception] = []
    if True:
        async with trio.open_nursery() as nursery:
            stream = make_stream(nursery)
            limiter = trio.Semaphore(in_flight)

            async def one_read(offset: int) -> None:
                try:
                    data = await stream.read(
                        request_start=offset,
                        request_end=offset + read_size - 1,
                        request_size=read_size,
                    )
                    assert data == DATA[offset : offset + read_size]
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                finally:
                    limiter.release()

            # Header first, as a player does.
            await stream.read(
                request_start=0, request_end=read_size - 1, request_size=read_size
            )
            for first in (read_size, *jumps):
                async with trio.open_nursery() as reads:
                    offset = first
                    while offset < first + total:
                        await limiter.acquire()
                        reads.start_soon(one_read, offset)
                        offset += read_size
            stream.is_killed.value = True
    return errors


def run_play(*args) -> list[Exception]:
    with tempfile.TemporaryDirectory() as cache_dir:
        di[Cache] = Cache(
            cfg=CacheConfig(
                cache_dir=Path(cache_dir),
                max_size_bytes=512 * MB,
                ttl_seconds=3600,
                eviction="LRU",
                metrics_enabled=False,
            )
        )
        di[ChunkCacheNotifier] = ChunkCacheNotifier()
        # Emitters are class-level; drop "cached" flags left by an earlier test's cache.
        ChunkCacheNotifier.emitters.clear()
        try:
            return trio.run(play, *args)
        finally:
            SlowBody.stall_at = None
            SlowBody.stall_error = httpx.ReadTimeout


def test_overlapping_sequential_reads_are_all_served():
    errors = run_play(8, 128 * 1024, 40 * MB)

    assert not [e for e in errors if isinstance(e, ChunksTooSlowException)], errors
    assert not errors, errors


def test_single_reads_still_work():
    assert run_play(1, 128 * 1024, 20 * MB) == []


def test_deep_read_window_is_all_served():
    # Samba + kernel readahead can keep several MB of reads in flight.
    errors = run_play(32, 128 * 1024, 40 * MB)

    assert not errors, [type(e).__name__ for e in errors][:5]


def test_seeking_forward_and_back_with_reads_in_flight():
    # Scrub forward, then back to the start, with a window of reads in flight.
    errors = run_play(16, 128 * 1024, 8 * MB, (40 * MB, 4 * MB))

    assert not errors, [type(e).__name__ for e in errors][:5]


def test_cdn_stall_mid_body_reconnects_instead_of_killing_the_stream():
    # A read timeout while reading the body used to make the stream fail every later read.
    SlowBody.stall_at = 6 * MB
    errors = run_play(8, 128 * 1024, 16 * MB)

    assert not errors, [type(e).__name__ for e in errors][:5]


def test_connection_reset_mid_body_reconnects():
    SlowBody.stall_at = 6 * MB
    SlowBody.stall_error = httpx.ReadError  # "Connection reset by peer"
    errors = run_play(8, 128 * 1024, 16 * MB)

    assert not errors, [type(e).__name__ for e in errors][:5]
