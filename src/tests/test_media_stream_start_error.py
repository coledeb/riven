"""A stream whose first connection fails must raise that error, not trio's RuntimeError."""

from types import SimpleNamespace

import pytest
import trio

from program.services.streaming.exceptions.debrid_service_exception import (
    DebridServiceRateLimitedException,
)
from program.services.streaming.media_stream import MediaStream


def make_stream(nursery, error):
    stream = MediaStream.__new__(MediaStream)
    stream.nursery = nursery
    stream.config = SimpleNamespace(connect_timeout_seconds=5)
    stream.is_streaming = SimpleNamespace(value=False)
    stream._stream_error = SimpleNamespace(value=None)
    stream.recent_reads = SimpleNamespace(
        current_read=SimpleNamespace(value=None),
        previous_read=SimpleNamespace(value=None),
    )

    async def detect_read_type(**_kwargs):
        return "body_read"

    async def run(*_args, **_kwargs):
        # Mirrors MediaStream.run(): a failed first connection is stored, then run() returns.
        stream._stream_error.value = error

    stream._detect_read_type = detect_read_type
    stream.run = run
    return stream


async def open_read(error):
    async with trio.open_nursery() as nursery:
        stream = make_stream(nursery, error)
        async with stream.read_lifecycle(SimpleNamespace(position=0)):
            pass


def test_failed_first_connection_raises_the_stream_error():
    error = DebridServiceRateLimitedException(provider="torbox")

    with pytest.raises(ExceptionGroup) as excinfo:
        trio.run(open_read, error)

    assert excinfo.group_contains(DebridServiceRateLimitedException)
    assert not excinfo.group_contains(RuntimeError)


def test_run_exiting_without_an_error_still_raises_runtime_error():
    with pytest.raises(ExceptionGroup) as excinfo:
        trio.run(open_read, None)

    assert excinfo.group_contains(RuntimeError)
