"""Known stream errors are logged as one readable line, not a traceback."""

import httpx

from program.services.streaming.exceptions import DebridServiceException
from program.services.streaming.media_stream import _error_summary


def status_error(code, url):
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        "boom\nFor more information check: https://developer.mozilla.org/x",
        request=request,
        response=httpx.Response(code, request=request),
    )


def raised_from(cause):
    try:
        raise DebridServiceException(
            "Unexpected HTTP 422 connecting to stream", provider="torbox"
        ) from cause
    except DebridServiceException as e:
        return e


def test_http_cause_is_one_line_without_the_query():
    e = raised_from(
        status_error(
            422,
            "https://api.torbox.app/v1/api/torrents/requestdl?token=SECRET&torrent_id=1",
        )
    )

    summary = _error_summary(e)

    assert "\n" not in summary
    assert "SECRET" not in summary and "mozilla" not in summary
    assert summary.endswith("(HTTP 422 from api.torbox.app/v1/api/torrents/requestdl)")


def test_other_cause_is_named_on_the_same_line():
    summary = _error_summary(raised_from(httpx.ReadTimeout("timed\nout")))

    assert "\n" not in summary
    assert summary.endswith("(ReadTimeout: timed out)")


def test_no_cause():
    summary = _error_summary(DebridServiceException("gone", provider="torbox"))

    assert summary.startswith("DebridServiceException: ")
