"""A CDN 429 must pause every connection to that host, not be retried at once."""

from types import SimpleNamespace

import httpx
import pytest
import trio
import trio.testing

from program.services.streaming import media_stream
from program.services.streaming.exceptions.debrid_service_exception import (
    DebridServiceRateLimitedException,
)
from program.services.streaming.media_stream import MediaStream

URL = "https://cdn.example/dld/file?token=t"


@pytest.fixture(autouse=True)
def clear_pauses():
    media_stream._host_paused_until.clear()
    media_stream._host_429_streak.clear()
    yield
    media_stream._host_paused_until.clear()
    media_stream._host_429_streak.clear()


def make_stream(handler):
    stream = MediaStream.__new__(MediaStream)
    stream.provider = "torbox"
    stream.target_url = SimpleNamespace(value=URL)
    stream.file_metadata = SimpleNamespace(file_size=10, original_filename="a.mkv")
    stream.session_statistics = SimpleNamespace(total_session_connections=0)
    stream.async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream.build_log_message = lambda message: message
    return stream


def run(fn):
    return trio.run(fn, clock=trio.testing.MockClock(autojump_threshold=0))


def test_429_waits_retry_after_then_succeeds():
    calls = []

    def handler(_request):
        calls.append(trio.current_time())
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(206, content=b"x" * 10)

    stream = make_stream(handler)

    async def main():
        async with stream.establish_connection(0, end=9) as response:
            return response.status_code

    assert run(main) == 206
    assert calls[1] - calls[0] >= 5


def test_429_without_retry_after_backs_off_then_gives_up():
    calls = []

    def handler(_request):
        calls.append(trio.current_time())
        return httpx.Response(429)

    stream = make_stream(handler)

    async def main():
        async with stream.establish_connection(0, end=9):
            pass

    with pytest.raises(DebridServiceRateLimitedException):
        run(main)
    gaps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    assert len(calls) == 4
    assert gaps == pytest.approx([2, 4, 8])


def test_429_pauses_other_connections_to_the_same_host():
    calls = []

    def handler(_request):
        calls.append(trio.current_time())
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "10"})
        return httpx.Response(206, content=b"x" * 10)

    first, second = make_stream(handler), make_stream(handler)

    async def connect(stream, delay):
        await trio.sleep(delay)
        async with stream.establish_connection(0, end=9):
            pass

    async def main():
        async with trio.open_nursery() as nursery:
            nursery.start_soon(connect, first, 0)
            nursery.start_soon(connect, second, 1)  # starts during the pause

    run(main)
    # Only the first request hit the CDN before the 10 s pause ended
    assert len(calls) == 3
    assert calls[1] >= calls[0] + 10 and calls[2] >= calls[0] + 10


def test_429s_in_a_row_keep_backing_off_across_connections():
    calls = []

    def handler(_request):
        calls.append(trio.current_time())
        return httpx.Response(429)

    stream = make_stream(handler)

    async def main():
        for _ in range(2):  # the reader tries again after the first give-up
            with pytest.raises(DebridServiceRateLimitedException):
                async with stream.establish_connection(0, end=9):
                    pass

    run(main)
    gaps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    # 2/4/8, then the second connection keeps growing instead of starting at 2 s
    assert gaps == pytest.approx([2, 4, 8, 16, 30, 30, 30])


def test_a_good_answer_resets_the_backoff():
    answers = [429, 429, 206, 429]

    def handler(_request):
        return httpx.Response(answers.pop(0), content=b"x" * 10)

    stream = make_stream(handler)

    async def main():
        async with stream.establish_connection(0, end=9):
            pass

        assert media_stream._host_429_streak == {}

        async with stream.establish_connection(0, end=9):
            pass

    answers.append(206)
    run(main)
    assert media_stream._host_429_streak == {}


def test_reconnect_uses_a_pushed_fresh_link(monkeypatch):
    fresh = "https://other-cdn.example/dld/file?token=new"
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(206, content=b"x" * 10)

    stream = make_stream(handler)
    stream._pushed_url = None
    monkeypatch.setattr(media_stream.stream_health, "pushed_url", lambda _key: fresh)

    async def main():
        async with stream.establish_connection(0, end=9):
            pass

    run(main)
    assert hosts == ["other-cdn.example"]
    assert stream.target_url.value == fresh
