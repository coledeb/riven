from types import SimpleNamespace

import pytest
from kink import di

from program.apis.overseerr_api import OverseerrAPI, OverseerrAPIError
from program.services import notifications as notifications_module
from program.services.notifications import NotificationService


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self._data = data or {}

    def json(self):
        return self._data


def api_with(response):
    api = object.__new__(OverseerrAPI)
    api.session = SimpleNamespace(get=lambda url, timeout=None: response)
    return api


def requests(*pairs):
    return {"mediaInfo": {"requests": [{"status": s, "is4k": k} for s, k in pairs]}}


@pytest.mark.parametrize(
    "data, expected",
    [
        (requests((1, False)), True),  # pending: approved when it turns available
        (requests((2, False)), True),
        (requests((4, False)), True),  # failed requests still complete
        (requests((5, False)), True),  # already announced
        (requests((3, False)), False),  # declined
        (requests((2, True)), False),  # 4K only
        (requests((3, False), (2, False)), True),
        ({"mediaInfo": None}, False),  # Seerr knows nothing about it
        ({}, False),
    ],
)
def test_has_request(data, expected):
    assert api_with(FakeResponse(200, data)).has_request("movie", "1") is expected


def test_has_request_unknown_title():
    assert api_with(FakeResponse(404)).has_request("tv", "1") is False


def test_has_request_error_raises():
    with pytest.raises(OverseerrAPIError):
        api_with(FakeResponse(500)).has_request("movie", "1")


class FakeSeerr:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def has_request(self, kind, tmdb_id):
        self.asked.append((kind, tmdb_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def seerr():
    previous = di[OverseerrAPI] if OverseerrAPI in di else None

    def install(answer):
        di[OverseerrAPI] = FakeSeerr(answer)
        return di[OverseerrAPI]

    yield install

    if previous is not None:
        di[OverseerrAPI] = previous


@pytest.fixture
def service(monkeypatch):
    sent = []
    svc = object.__new__(NotificationService)
    svc.settings = SimpleNamespace(
        enabled=True,
        on_item_type=["movie", "show"],
        service_urls=["https://example/hook"],
        link_url="",
        skip_seerr_requests=True,
    )
    monkeypatch.setattr(notifications_module, "send", lambda urls, notice: sent.append(notice))
    svc.sent = sent
    return svc


def movie(requested_by="torbox_import", tmdb_id="603"):
    return SimpleNamespace(
        type="movie",
        title="The Matrix",
        log_string="The Matrix",
        aired_at=None,
        tmdb_id=tmdb_id,
        tvdb_id=None,
        imdb_id="tt0133093",
        requested_by=requested_by,
        poster_path=None,
        parent=None,
    )


def test_unrequested_item_is_announced(service, seerr):
    fake = seerr(False)
    service._send_external_notification(movie())
    assert fake.asked == [("movie", "603")]
    assert len(service.sent) == 1


def test_seerr_requested_item_is_skipped(service, seerr):
    seerr(True)
    service._send_external_notification(movie())
    assert service.sent == []


def test_toggle_off_announces_everything(service, seerr):
    fake = seerr(True)
    service.settings.skip_seerr_requests = False
    service._send_external_notification(movie())
    assert fake.asked == []
    assert len(service.sent) == 1


def test_lookup_failure_falls_back_to_source(service, seerr):
    seerr(OverseerrAPIError("down"))
    service._send_external_notification(movie(requested_by="overseerr"))
    service._send_external_notification(movie())
    assert len(service.sent) == 1


def test_show_without_tmdb_uses_tvdb_mapping(service, seerr, monkeypatch):
    fake = seerr(False)
    monkeypatch.setattr(notifications_module, "_tmdb_for_tvdb", lambda tvdb: "1399")
    show = movie()
    show.type, show.tmdb_id, show.tvdb_id = "show", None, "121361"
    service._send_external_notification(show)
    assert fake.asked == [("tv", "1399")]
