import base64
import json
import threading

import httpx
import pytest
from RTN import parse

from program.media.item import Movie
from program.services.downloaders import torbox
from program.services.scrapers import debridio
from program.services.scrapers.debridio import (
    Debridio,
    hash_from_url,
    language_tags,
    torrent_title,
)
from program.settings import settings_manager
from program.utils.request import SmartSession

PACK = "a" * 40
SINGLE = "b" * 40
UNCACHED = "c" * 40
MISNAMED = "d" * 40


def stream(infohash, filename):
    return {
        "name": "[TB ⚡] \nDebridio 1080p",
        "title": f"{filename}\n⚡ 📦 📺 1080p 💾 1.5 GB  ",
        "url": f"https://addon.debridio.com/play/movie/torbox/KEY1/KEY2/{infohash}/{filename}",
        "behaviorHints": {"filename": filename},
    }


def files(*names):
    return [{"name": n, "size": 1} for n in names]


class FakeAPI:
    def __init__(self):
        self.streams = [
            stream(PACK, "Movie.2000.1080p.BluRay.mkv"),
            stream(SINGLE, "Movie.2000.2160p.WEB-DL.mkv"),
            stream(UNCACHED, "Movie.2000.720p.mkv"),
        ]
        self.cached = {
            PACK: {
                "name": "Movie Collection 1080p BluRay",
                "files": files("a.mkv", "b.mkv"),
            },
            SINGLE: {
                "name": "Movie.2000.2160p.WEB-DL",
                "files": files("Movie.2000.2160p.mkv"),
            },
        }
        self.torbox_status = 200
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        host, path = request.url.host, request.url.path

        if host == "addon.debridio.com" and path.endswith("/manifest.json"):
            return httpx.Response(200, json={"name": "Debridio - Scraper TB"})
        if host == "addon.debridio.com" and "/stream/" in path:
            return httpx.Response(200, json={"streams": self.streams})
        if host == "api.torbox.app" and path.endswith("/torrents/checkcached"):
            if self.torbox_status != 200:
                return httpx.Response(
                    self.torbox_status, headers={"Retry-After": "300"}
                )
            hashes = request.url.params["hash"].split(",")
            data = {h: self.cached[h] for h in hashes if h in self.cached}
            return httpx.Response(200, json={"success": True, "data": data})

        return httpx.Response(404)


@pytest.fixture
def api(monkeypatch):
    api = FakeAPI()

    # Riven registers these levels at startup; the tests run without it.
    for level in ("SCRAPER", "NOT_FOUND"):
        try:
            debridio.logger.level(level)
        except ValueError:
            debridio.logger.level(level, no=20)

    class FakeSession(SmartSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._client = httpx.Client(transport=httpx.MockTransport(api.handler))

    monkeypatch.setattr(debridio, "SmartSession", FakeSession)
    monkeypatch.setattr(torbox, "SmartSession", FakeSession)

    settings = settings_manager.settings.scraping.debridio
    monkeypatch.setattr(settings, "enabled", True)
    monkeypatch.setattr(settings, "api_key", "debridio-key")
    monkeypatch.setattr(
        settings_manager.settings.downloaders.torbox, "api_key", "torbox-key"
    )

    return api


@pytest.fixture
def downloader(api):  # noqa: ARG001 - installs the fake API first
    """A running TorBox downloader without the startup account checks."""

    downloader = torbox.TorBoxDownloader.__new__(torbox.TorBoxDownloader)
    downloader.key = "torbox"
    downloader.api = torbox.TorBoxAPI("torbox-key")
    downloader._lock = threading.Lock()
    downloader._paused_until = {}
    downloader.initialized = True
    return downloader


@pytest.fixture
def scraper(downloader, monkeypatch):
    monkeypatch.setattr(Debridio, "_torbox", staticmethod(lambda: downloader))
    scraper = Debridio()
    assert scraper.initialized
    return scraper


def torbox_calls(api):
    return sum(1 for r in api.calls if r.url.host == "api.torbox.app")


def movie():
    return Movie({"imdb_id": "tt0111161", "title": "Movie", "type": "movie"})


def test_hash_comes_from_url_path():
    url = (
        f"https://addon.debridio.com/play/series/torbox/K1/K2/{PACK.upper()}/S03E02.mkv"
    )
    assert hash_from_url(url) == PACK
    assert hash_from_url("https://addon.debridio.com/play/no-hash/here.mkv") is None


def test_config_segment_matches_debridio_format(scraper):
    config = json.loads(base64.b64decode(scraper._config_segment()))
    assert config["api_key"] == "debridio-key"
    assert config["provider"] == "torbox"
    assert config["providerKey"] == "torbox-key"


def test_titles_come_from_torbox_and_uncached_are_dropped(scraper):
    results = scraper.scrape(movie())

    assert results == {
        PACK: "Movie Collection 1080p BluRay",
        SINGLE: "Movie.2000.2160p.WEB-DL",
    }


def test_falls_back_to_debridio_titles_when_torbox_is_throttled(scraper, api):
    api.torbox_status = 429

    results = scraper.scrape(movie())

    assert results[UNCACHED] == "Movie.2000.720p.mkv"
    assert len(results) == 3
    # One TorBox call only: no inline sleep/retry on the 429.
    assert torbox_calls(api) == 1

    # The 429 paused the shared downloader, so the next scrape skips TorBox.
    assert len(scraper.scrape(movie())) == 3
    assert torbox_calls(api) == 1


def test_downloader_pause_also_pauses_debridio(scraper, api, downloader):
    downloader._paused_until["api"] = float("inf")

    results = scraper.scrape(movie())

    assert len(results) == 3
    assert torbox_calls(api) == 0


def test_checks_use_the_downloader_session(scraper, api):
    scraper.scrape(movie())

    (call,) = [r for r in api.calls if r.url.host == "api.torbox.app"]
    assert call.headers["Authorization"] == "Bearer torbox-key"
    assert call.url.path == "/v1/api/torrents/checkcached"


def test_without_torbox_downloader_debridio_titles_are_used(api, monkeypatch):
    monkeypatch.setattr(Debridio, "_torbox", staticmethod(lambda: None))

    results = Debridio().scrape(movie())

    assert len(results) == 3
    assert torbox_calls(api) == 0


def test_large_answers_are_capped_and_batched(scraper, api, monkeypatch):
    monkeypatch.setattr(debridio, "MAX_HASHES", 250)
    api.streams = [stream(f"{i:040x}", f"Movie.{i}.mkv") for i in range(400)]

    scraper.scrape(movie())

    torbox_calls = [r for r in api.calls if r.url.host == "api.torbox.app"]
    assert len(torbox_calls) == 3
    assert sum(len(r.url.params["hash"].split(",")) for r in torbox_calls) == 250


def test_keys_never_reach_the_log(scraper, api, monkeypatch):
    logged = []
    monkeypatch.setattr(
        debridio.logger, "error", lambda msg, *_a, **_k: logged.append(msg)
    )

    def boom(request):
        raise httpx.ConnectError(f"failed {request.url}")

    api.handler = boom
    scraper.session._client = httpx.Client(transport=httpx.MockTransport(boom))

    assert scraper.run(movie()) == {}
    assert logged and not any("debridio-key" in m or "torbox-key" in m for m in logged)
    assert not any(scraper._config_segment() in m for m in logged)


def test_single_episode_name_on_a_series_pack_gets_the_season_range():
    name = "Gilmore.Girls.S05E01.Say.Goodbye.to.Daisy.Miller.1080p.WEB-DL.x265-MONOLITH.mkv"
    pack = files(
        *(
            f"Season {s}/Gilmore.Girls.S0{s}E{e:02d}.1080p.mkv"
            for s in range(1, 8)
            for e in range(1, 4)
        )
    )

    title = torrent_title(name, pack)

    assert (
        title
        == "Gilmore.Girls.S01-S07.Say.Goodbye.to.Daisy.Miller.1080p.WEB-DL.x265-MONOLITH"
    )


def test_correct_names_are_left_alone():
    season_pack = "Gilmore Girls S03 1080p WEB-DL DD+ 2.0 x264-TrollHD"
    assert torrent_title(season_pack, files("S03E01.mkv", "S03E02.mkv")) == season_pack

    single = "Show.S01E02.1080p.WEB-DL.mkv"
    assert (
        torrent_title(single, files("Show.S01E02.1080p.WEB-DL.mkv", "sample.txt"))
        == single
    )



def test_uploader_tag_is_removed_so_rtn_keeps_the_release():
    name = "Reasonable.Doubt.2022.S03E08.1080p.WEB.h264-ETHEL[EZTVx.to].mkv"
    title = torrent_title(name, files(name))

    assert title == "Reasonable.Doubt.2022.S03E08.1080p.WEB.h264-ETHEL.mkv"
    assert parse(name).site and not parse(title).site
    assert torrent_title("Show.S01E01.720p-GRP [eztv.re]", []) == "Show.S01E01.720p-GRP"


def test_other_brackets_are_left_alone():
    for name in (
        "Show S01E01 1080p WEB-DL [Dual Audio].mkv",
        "Show S01E01 [1080p] [x265].mkv",
        "Show.S01E01.H.264-[GRP].mkv",
    ):
        assert torrent_title(name, []) == name


def test_debridio_titles_never_replace_another_scrapers_title():
    from program.services.scrapers import merge_results

    torrentio = {PACK: "Show S01E01 1080p WEB h264-ETHEL"}
    tagged = {PACK: "Show.S01E01.1080p.WEB.h264-ETHEL[EZTVx.to].mkv", SINGLE: "Other"}

    first = dict(torrentio)
    merge_results(first, "debridio", tagged)
    second = {}
    merge_results(second, "debridio", tagged)
    merge_results(second, "torrentio", torrentio)

    assert first == second == {PACK: torrentio[PACK], SINGLE: "Other"}

def test_language_flags_become_words_riven_can_filter():
    title = "Movie.2000.1080p.mkv\n⚡ 📦 📺 1080p 💾 1.5 GB  \n🌐 Multi Audio|🇫🇷|🇲🇽"
    assert language_tags(title) == "MULTI FRENCH LATINO"
    assert language_tags("Movie.2000.1080p.mkv\n🌐 Multi Subs") == ""
    assert language_tags("Movie.2000.1080p.mkv\n⚡ 1080p") == ""


def test_languages_are_appended_to_torbox_names(scraper, api):
    api.streams[0]["title"] += "\n🌐 🇬🇧|🇷🇺"

    results = scraper.scrape(movie())

    assert results[PACK] == "Movie Collection 1080p BluRay [ENGLISH RUSSIAN]"
    assert parse(results[PACK]).languages == ["en", "ru"]
    assert results[SINGLE] == "Movie.2000.2160p.WEB-DL"


def test_rejected_key_says_so(scraper, api, monkeypatch):
    logged = []
    monkeypatch.setattr(
        debridio.logger, "error", lambda msg, *_a, **_k: logged.append(msg)
    )
    api.handler = lambda _request: httpx.Response(
        403, json={"message": "Subscription is inactive. Server denied access."}
    )
    scraper.session._client = httpx.Client(transport=httpx.MockTransport(api.handler))

    assert scraper.scrape(movie()) == {}
    assert "rejected the API key or the subscription is inactive" in logged[0]
