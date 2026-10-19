import json
import socket
from urllib.parse import parse_qs

import httpx
import pytest

from program.services.downloaders import torbox
from program.services.downloaders.torbox import TorBoxDownloader, TorBoxRateLimited
from program.services.streaming.exceptions.debrid_service_exception import (
    DebridServiceLinkUnavailable,
)
from program.settings import settings_manager
from program.utils.request import CircuitBreakerOpen, SmartSession

CACHED = "a" * 40
UNCACHED = "b" * 40
EXISTING = "c" * 40
EPISODE_BYTES = 500_000_000


def torrent(torrent_id, infohash, files, finished=True):
    return {
        "id": torrent_id,
        "hash": infohash,
        "name": f"Show.S01.1080p-{torrent_id}",
        "size": sum(f["size"] for f in files),
        "progress": 1 if finished else 0.4,
        "download_state": "cached" if finished else "downloading",
        "download_finished": finished,
        "created_at": "2026-10-17T12:00:00Z",
        "expires_at": None,
        "files": files,
    }


def episode_files(count=2):
    return [
        {
            "id": i,
            "name": f"Show.S01.1080p/Show.S01E{i + 1:02d}.1080p.mkv",
            "short_name": f"Show.S01E{i + 1:02d}.1080p.mkv",
            "size": EPISODE_BYTES,
        }
        for i in range(count)
    ]


class FakeTorBox:
    """In-memory stand-in for the TorBox API endpoints the downloader uses."""

    def __init__(self):
        self.plan = 3
        self.cached = {CACHED: episode_files()}
        self.torrents = {}
        self.next_id = 100
        self.calls = []
        self.create_response = None
        self.requestdl_response = None

    def add_existing(self, infohash, files):
        self.torrents[self.next_id] = torrent(self.next_id, infohash, files)
        self.next_id += 1
        return self.next_id - 1

    def count(self, path):
        return sum(1 for method, p, _ in self.calls if p == path)

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: C901, PLR0912
        path = request.url.path.removeprefix("/v1/api/")
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((request.method, path, query))

        def ok(data, detail="ok"):
            return httpx.Response(
                200,
                json={"success": True, "error": None, "detail": detail, "data": data},
            )

        if path == "user/me":
            return ok(
                {
                    "id": 7,
                    "email": "user@example.com",
                    "plan": self.plan,
                    "premium_expires_at": "2027-01-01T00:00:00Z",
                    "cooldown_until": None,
                    "total_bytes_downloaded": 123,
                }
            )

        if path == "torrents/checkcached":
            hashes = query["hash"].split(",")
            return ok(
                {
                    h: {"name": "x", "size": 1, "hash": h}
                    for h in hashes
                    if h in self.cached
                }
            )

        if path == "torrents/mylist":
            if "id" in query:
                t = self.torrents.get(int(query["id"]))
                if not t:
                    return httpx.Response(
                        404,
                        json={
                            "success": True,
                            "error": "ITEM_NOT_FOUND",
                            "detail": "No torrents found for this user.",
                            "data": {},
                        },
                    )
                return ok(t)
            items = sorted(self.torrents.values(), key=lambda t: -t["id"])
            offset, limit = int(query.get("offset", 0)), int(query.get("limit", 1000))
            return ok(items[offset : offset + min(limit, 1000)])

        if path == "torrents/createtorrent":
            if self.create_response:
                return self.create_response
            form = parse_qs(request.content.decode())
            infohash = form["magnet"][0].rsplit(":", 1)[-1]
            assert form.get("add_only_if_cached") == ["true"]
            for t in self.torrents.values():
                if t["hash"] == infohash:
                    return ok({"torrent_id": t["id"], "hash": infohash, "auth_id": "x"})
            if infohash not in self.cached:
                return httpx.Response(
                    200,
                    json={
                        "success": False,
                        "error": "DOWNLOAD_NOT_CACHED",
                        "detail": "Not cached",
                        "data": None,
                    },
                )
            torrent_id = self.add_existing(infohash, self.cached[infohash])
            return ok({"torrent_id": torrent_id, "hash": infohash, "auth_id": "x"})

        if path == "torrents/controltorrent":
            body = json.loads(request.content)
            assert body["operation"] == "delete"
            self.torrents.pop(body["torrent_id"], None)
            return httpx.Response(
                200, json={"success": True, "detail": "Torrent deleted successfully"}
            )

        if path == "torrents/requestdl":
            if self.requestdl_response:
                return self.requestdl_response
            return ok(
                f"https://cdn.example/{query['torrent_id']}/{query['file_id']}?sig=1"
            )

        return httpx.Response(
            500, json={"success": False, "error": "UNEXPECTED", "detail": path}
        )


@pytest.fixture
def fake(monkeypatch):
    fake = FakeTorBox()

    class FakeSession(SmartSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._client = httpx.Client(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(torbox, "SmartSession", FakeSession)
    # Keep tests fast: no pacing between link requests.
    monkeypatch.setattr(torbox, "LINK_BURST", 1000)
    monkeypatch.setattr(torbox.time, "sleep", lambda _s: None)

    tb_settings = settings_manager.settings.downloaders.torbox
    monkeypatch.setattr(tb_settings, "enabled", True)
    monkeypatch.setattr(tb_settings, "api_key", "secret-key")

    return fake


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """CDN host lookups without the network: dead.cdn is a pulled server, nodns.cdn fails."""

    def getaddrinfo(host, *_args, **_kwargs):
        if host == "nodns.cdn":
            raise socket.gaierror("lookup failed")
        ip = "0.0.0.0" if host == "dead.cdn" else "203.0.113.7"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(torbox.socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def downloader(fake):  # noqa: ARG001 - installs the fake API first
    downloader = TorBoxDownloader()
    assert downloader.initialized
    return downloader


def test_requires_paid_plan(fake):
    fake.plan = 0
    assert not TorBoxDownloader().initialized


def test_user_info(downloader):
    info = downloader.get_user_info()
    assert info.service == "torbox"
    assert info.premium_status == "premium"
    assert info.premium_days_left is not None


def test_uncached_hash_is_not_added(downloader, fake):
    assert downloader.get_instant_availability(UNCACHED, "show") is None
    assert fake.count("torrents/createtorrent") == 0


def test_cached_hash_is_added_with_files(downloader, fake):
    container = downloader.get_instant_availability(CACHED.upper(), "show")

    assert container is not None
    assert container.torrent_id in {str(i) for i in fake.torrents}
    assert [f.filename for f in container.files] == [
        "Show.S01E01.1080p.mkv",
        "Show.S01E02.1080p.mkv",
    ]
    assert container.torrent_info.status == "downloaded"

    link = container.files[0].download_url
    assert link.startswith("https://api.torbox.app/v1/api/torrents/requestdl?")
    assert "secret-key" not in link
    assert fake.count("torrents/createtorrent") == 1


def test_existing_torrent_is_reused_and_never_deleted(downloader, fake):
    existing_id = fake.add_existing(EXISTING, episode_files())
    fake.cached[EXISTING] = episode_files()

    container = downloader.get_instant_availability(EXISTING, "show")

    assert container.torrent_id == str(existing_id)
    assert fake.count("torrents/createtorrent") == 0

    downloader.delete_torrent(existing_id)
    assert fake.count("torrents/controltorrent") == 0
    assert existing_id in fake.torrents


def test_created_torrent_without_usable_files_is_removed(downloader, fake, monkeypatch):
    monkeypatch.setattr(downloader, "_torrent_in_use", lambda _id: False)
    fake.cached[CACHED] = [
        {"id": 0, "name": "Show/readme.txt", "short_name": "readme.txt", "size": 10}
    ]

    assert downloader.get_instant_availability(CACHED, "show") is None
    assert fake.count("torrents/controltorrent") == 1
    assert fake.torrents == {}


def test_created_torrent_in_use_is_kept(downloader, fake, monkeypatch):
    # A season pack Riven added: one episode fails on it while another uses it
    fake.cached[CACHED] = episode_files()
    container = downloader.get_instant_availability(CACHED, "show")
    monkeypatch.setattr(downloader, "_torrent_in_use", lambda _id: True)

    downloader.delete_torrent(container.torrent_id)

    assert fake.count("torrents/controltorrent") == 0
    assert int(container.torrent_id) in fake.torrents


def test_created_torrent_not_in_use_is_deleted(downloader, fake, monkeypatch):
    fake.cached[CACHED] = episode_files()
    container = downloader.get_instant_availability(CACHED, "show")
    monkeypatch.setattr(downloader, "_torrent_in_use", lambda _id: False)

    downloader.delete_torrent(container.torrent_id)

    assert fake.count("torrents/controltorrent") == 1
    assert fake.torrents == {}


def test_existing_torrent_without_usable_files_is_kept(downloader, fake):
    existing_id = fake.add_existing(
        EXISTING,
        [{"id": 0, "name": "x/readme.txt", "short_name": "readme.txt", "size": 10}],
    )
    fake.cached[EXISTING] = []

    assert downloader.get_instant_availability(EXISTING, "show") is None
    assert fake.count("torrents/controltorrent") == 0
    assert existing_id in fake.torrents


def test_account_list_is_paged(downloader, fake):
    for i in range(1001):
        fake.add_existing(f"{i:040x}", episode_files(1))
    target = f"{0:040x}"  # oldest torrent, only on the second page
    fake.cached[target] = episode_files(1)

    container = downloader.get_instant_availability(target, "show")

    assert container is not None
    assert fake.count("torrents/createtorrent") == 0
    offsets = [
        q.get("offset")
        for m, p, q in fake.calls
        if p == "torrents/mylist" and "id" not in q
    ]
    assert offsets == ["0", "1000"]
    assert all(
        q.get("bypass_cache") == "true"
        for m, p, q in fake.calls
        if p == "torrents/mylist"
    )


def test_create_list_response(downloader, fake):
    fake.create_response = httpx.Response(
        200,
        json={
            "success": True,
            "data": [{"id": 5, "hash": "other"}, {"id": 6, "hash": CACHED}],
        },
    )
    assert downloader.add_torrent(CACHED) == "6"


def test_add_limit_pauses_adds(downloader, fake):
    fake.create_response = httpx.Response(
        429,
        headers={"Retry-After": "300"},
        json={
            "success": False,
            "error": "RATE_LIMIT_EXCEEDED",
            "detail": "60 per 1 hour",
            "data": None,
        },
    )

    with pytest.raises(CircuitBreakerOpen):
        downloader.get_instant_availability(CACHED, "show")

    calls = fake.count("torrents/createtorrent")

    with pytest.raises(TorBoxRateLimited):
        downloader.add_torrent(CACHED)

    assert fake.count("torrents/createtorrent") == calls
    # Other API calls are unaffected by the add pause.
    assert downloader.get_user_info() is not None


def test_unrestrict_link(downloader, fake):
    container = downloader.get_instant_availability(CACHED, "show")
    link = container.files[1].download_url

    result = downloader.unrestrict_link(link)

    assert result.download == f"https://cdn.example/{container.torrent_id}/1?sig=1"
    _, _, query = fake.calls[-1]
    assert query == {
        "token": "secret-key",
        "torrent_id": container.torrent_id,
        "file_id": "1",
        "redirect": "false",
    }


def test_unrestrict_link_for_removed_torrent(downloader, fake):
    fake.requestdl_response = httpx.Response(
        500, json={"success": False, "error": "DATABASE_ERROR", "data": None}
    )

    with pytest.raises(DebridServiceLinkUnavailable):
        downloader.unrestrict_link(downloader._file_link(999, 0))


def test_unrestrict_link_transient_error(downloader, fake):
    existing_id = fake.add_existing(EXISTING, episode_files())
    fake.requestdl_response = httpx.Response(
        500, json={"success": False, "error": "DATABASE_ERROR", "data": None}
    )

    assert downloader.unrestrict_link(downloader._file_link(existing_id, 0)) is None


def test_unrestrict_link_rate_limit(downloader, fake):
    existing_id = fake.add_existing(EXISTING, episode_files())
    fake.requestdl_response = httpx.Response(
        429, headers={"Retry-After": "300"}, json={"success": False}
    )

    assert downloader.unrestrict_link(downloader._file_link(existing_id, 0)) is None
    calls = fake.count("torrents/requestdl")

    assert downloader.unrestrict_link(downloader._file_link(existing_id, 0)) is None
    assert fake.count("torrents/requestdl") == calls


@pytest.mark.parametrize(
    ("host", "dead"), [("dead.cdn", True), ("nodns.cdn", False), ("live.cdn", False)]
)
def test_unrestrict_link_on_offline_cdn_server(downloader, fake, host, dead):
    existing_id = fake.add_existing(EXISTING, episode_files())
    fake.requestdl_response = httpx.Response(
        200, json={"success": True, "data": f"https://{host}/dld/x?token=t"}
    )
    link = downloader._file_link(existing_id, 0)

    if dead:
        with pytest.raises(DebridServiceLinkUnavailable):
            downloader.unrestrict_link(link)
    else:
        assert downloader.unrestrict_link(link).download.startswith(f"https://{host}/")


@pytest.fixture
def manage_all(downloader, monkeypatch):
    monkeypatch.setattr(downloader.settings, "only_manage_own_torrents", False)
    return downloader


def test_existing_torrent_deleted_when_managing_all(manage_all, fake, monkeypatch):
    existing_id = fake.add_existing(EXISTING, episode_files())
    monkeypatch.setattr(manage_all, "_torrent_in_use", lambda _id: False)

    manage_all.delete_torrent(existing_id)

    assert fake.count("torrents/controltorrent") == 1
    assert existing_id not in fake.torrents


def test_torrent_in_library_kept_when_managing_all(manage_all, fake, monkeypatch):
    existing_id = fake.add_existing(EXISTING, episode_files())
    monkeypatch.setattr(manage_all, "_torrent_in_use", lambda _id: True)

    manage_all.delete_torrent(existing_id)

    assert fake.count("torrents/controltorrent") == 0
    assert existing_id in fake.torrents


def test_torrent_in_use_when_database_unavailable(downloader, monkeypatch):
    def broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(torbox, "db_session", broken)
    assert downloader._torrent_in_use("123") is True
