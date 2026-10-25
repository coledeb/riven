"""A failed link refresh is retried briefly, and never falls back to the stored
provider link (TorBox: an API URL without the key, which answers 422)."""

from types import SimpleNamespace

import pytest
import trio
from kink import di

from program.services.filesystem.vfs import VFSDatabase
from program.services.filesystem.vfs.db import GetEntryByOriginalFilenameResult
from program.services.streaming import media_stream
from program.services.streaming.media_stream import MediaStream

STALE = "https://cdn.example/dld/old?token=t"
FRESH = "https://cdn.example/dld/new?token=t"
STORED = "https://api.torbox.app/v1/api/torrents/requestdl?torrent_id=1&file_id=0"


def result(unrestricted_url=None, link_gone=False):
    return GetEntryByOriginalFilenameResult(
        original_filename="a.mkv",
        download_url=STORED,
        unrestricted_url=unrestricted_url,
        provider="torbox",
        provider_download_id="1",
        size=10,
        created=None,
        modified=None,
        entry_type="media",
        link_gone=link_gone,
    )


@pytest.fixture
def stream(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(media_stream.trio, "sleep", fake_sleep)

    s = MediaStream.__new__(MediaStream)
    s.provider = "torbox"
    s.target_url = SimpleNamespace(value=STALE)
    s.file_metadata = SimpleNamespace(file_size=10, original_filename="a.mkv")
    s.build_log_message = lambda message: message
    s.enable_tracing = False
    s.sleeps = sleeps
    return s


def use_results(*results):
    calls = []

    def get_entry(**kwargs):
        calls.append(kwargs)
        return results[min(len(calls), len(results)) - 1]

    di[VFSDatabase] = SimpleNamespace(get_entry_by_original_filename=get_entry)
    return calls


@pytest.fixture(autouse=True)
def restore_di():
    original = di[VFSDatabase] if VFSDatabase in di else None
    yield
    if original is not None:
        di[VFSDatabase] = original


def test_url_never_falls_back_to_the_stored_link():
    assert result().url is None
    assert result(FRESH).url == FRESH


def test_failed_refresh_is_retried_then_succeeds(stream):
    calls = use_results(result(), result(), result(FRESH))

    assert trio.run(stream._refresh_download_url) is True
    assert stream.target_url.value == FRESH
    assert len(calls) == 3
    assert stream.sleeps == [2.0, 4.0]


def test_refresh_gives_up_after_two_retries(stream):
    calls = use_results(result())

    assert trio.run(stream._refresh_download_url) is False
    assert stream.target_url.value == STALE
    assert len(calls) == 3


def test_gone_link_is_not_retried(stream):
    calls = use_results(result(link_gone=True))

    assert trio.run(stream._refresh_download_url) is False
    assert len(calls) == 1
    assert stream.sleeps == []
