"""An expired CDN link must be refreshed, not treated as a fatal stream error."""

from types import SimpleNamespace

import httpx
import pytest
import trio

from program.services.streaming.exceptions.debrid_service_exception import (
    DebridServiceException,
)
from program.services.streaming.media_stream import MediaStream

STALE = "https://cdn.example/dld/old?token=t"
FRESH = "https://cdn.example/dld/new?token=t"


def make_stream(refreshed_url):
    def handler(request):
        if str(request.url) == FRESH:
            return httpx.Response(206, content=b"x" * 10)
        return httpx.Response(400, content=b"bad request")

    stream = MediaStream.__new__(MediaStream)
    stream.provider = "torbox"
    stream.target_url = SimpleNamespace(value=STALE)
    stream.file_metadata = SimpleNamespace(file_size=10, original_filename="a.mkv")
    stream.session_statistics = SimpleNamespace(total_session_connections=0)
    stream.async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream.build_log_message = lambda message: message
    stream.refreshes = 0

    async def refresh():
        stream.refreshes += 1
        if refreshed_url is None:
            return False
        stream.target_url.value = refreshed_url
        return True

    async def no_wait(attempt, max_attempts, _backoffs):
        return attempt < max_attempts - 1

    stream._refresh_download_url = refresh
    stream._retry_with_backoff = no_wait
    return stream


async def connect(stream):
    async with stream.establish_connection(0, end=9) as response:
        return response.status_code


def test_http_400_refreshes_the_link_and_retries():
    stream = make_stream(FRESH)

    assert trio.run(connect, stream) == 206
    assert stream.refreshes == 1
    assert stream.target_url.value == FRESH


def test_http_400_without_a_fresh_link_still_fails():
    stream = make_stream(None)

    with pytest.raises(DebridServiceException):
        trio.run(connect, stream)
    assert stream.refreshes == 1
