"""Debrid CDN URLs carry the account key (TorBox ?token=<key>); logs must not."""

import httpx
from loguru import logger

from program.utils.logging import _redact_record, redact_secrets

KEY = "4dbe4861-0000-4000-8000-000000000000"
URL = f"https://nexus-1.cnam.tb-cdn.io/dld/abc?token={KEY}"


def test_redact_secrets_query_params():
    assert KEY not in redact_secrets(URL)
    assert "token=REDACTED" in redact_secrets(URL)
    assert (
        redact_secrets("https://x/api?a=1&apikey=SECRET&b=2")
        == "https://x/api?a=1&apikey=REDACTED&b=2"
    )
    # plain text and key-less URLs are untouched
    assert redact_secrets("token is fine") == "token is fine"
    assert redact_secrets("https://x/a?file_id=3") == "https://x/a?file_id=3"


def test_logged_message_and_exception_are_redacted():
    out: list[str] = []
    request = httpx.Request("GET", URL)
    response = httpx.Response(400, request=request)
    handler = logger.add(
        out.append, format="{message}", backtrace=False, diagnose=False
    )
    patched = logger.patch(_redact_record)
    try:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            patched.warning(f"HTTP error 400: {e}")
            patched.exception("stream failed")
    finally:
        logger.remove(handler)
    text = "".join(out)
    assert "HTTP error 400" in text and "HTTPStatusError" in text
    assert KEY not in text
