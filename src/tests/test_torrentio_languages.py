import httpx
import pytest

from program.media.item import Movie
from program.services.scrapers import torrentio
from program.services.scrapers.languages import flag_languages
from program.services.scrapers.torrentio import Torrentio
from program.settings import settings_manager
from program.utils.request import SmartSession

# Real Torrentio title formats (2026-10-18).
RU_DUB = "a" * 40
ENGLISH = "b" * 40
NO_FLAGS = "c" * 40
STREAMS = [
    {
        "infoHash": RU_DUB,
        "title": "Побег из Шоушенка / The Shawshank Redemption [1994 BDRip] MVO\n👤 13 💾 2.18 GB ⚙️ Rutracker\n🇷🇺",
    },
    {
        "infoHash": ENGLISH,
        "title": "The.Shawshank.Redemption.1994.1080p.Bluray.MULTi\nThe.Shawshank.Redemption.1994.mkv\n👤 17 💾 19.56 GB ⚙️ ThePirateBay\nMulti Audio / 🇬🇧 / 🇮🇹 / 🇲🇽",
    },
    {
        "infoHash": NO_FLAGS,
        "title": "Gilmore Girls S03 1080p AMZN WEB-DL DD+ 2.0 x264-TrollHD\nGilmore Girls S03E02.mkv\n👤 37 💾 4.48 GB ⚙️ ThePirateBay",
    },
]


@pytest.fixture
def scraper(monkeypatch):
    def handler(request):
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json={"name": "Torrentio"})
        return httpx.Response(200, json={"streams": STREAMS})

    class FakeSession(SmartSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._client = httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(torrentio, "SmartSession", FakeSession)
    monkeypatch.setattr(settings_manager.settings.scraping.torrentio, "enabled", True)
    for level in ("SCRAPER", "NOT_FOUND"):
        try:
            torrentio.logger.level(level)
        except ValueError:
            torrentio.logger.level(level, no=20)

    scraper = Torrentio()
    assert scraper.initialized
    return scraper


def test_torrentio_titles_keep_their_languages(scraper):
    results = scraper.scrape(Movie({"imdb_id": "tt0111161", "type": "movie"}))

    assert results == {
        RU_DUB: "Побег из Шоушенка / The Shawshank Redemption [1994 BDRip] MVO [RUSSIAN]",
        ENGLISH: "The.Shawshank.Redemption.1994.1080p.Bluray.MULTi [MULTI ENGLISH ITALIAN LATINO]",
        NO_FLAGS: "Gilmore Girls S03 1080p AMZN WEB-DL DD+ 2.0 x264-TrollHD",
    }


def test_flag_line_formats():
    assert flag_languages("🇬🇧 / 🇷🇺") == "ENGLISH RUSSIAN"
    assert flag_languages("Dual Audio / 🇬🇧 / 🇵🇹") == "DUAL ENGLISH PORTUGUESE"
    assert flag_languages("🌐 Multi Audio|🇫🇷") == "MULTI FRENCH"
    assert flag_languages("Multi Subs / 🇬🇧 / 🇮🇳") == "ENGLISH HINDI"
    assert flag_languages("") == ""
