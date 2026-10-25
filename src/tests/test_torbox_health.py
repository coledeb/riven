"""TorBox degraded hold (downloaders.torbox.degraded_hold) and status page."""

import time
from types import SimpleNamespace

import httpx
import pytest

from program.services.downloaders import torbox_health, torbox_uncached
from program.services.downloaders.torbox_health import (
    PageStatus,
    TorBoxHealth,
    TorBoxStatusPage,
    health,
    region_of,
)
from program.services.downloaders.torbox_import import TorBoxAccountImport
from program.services.streaming.exceptions import DebridServiceLinkUnavailable
from program.settings import settings_manager
from program.utils.notify import GREEN, ORANGE, RED
from tests.test_torbox_downloader import (  # noqa: F401 - fixtures
    CACHED,
    EXISTING,
    downloader,
    episode_files,
    fake,
    fake_dns,
)
from tests.test_torbox_uncached import (  # noqa: F401 - fixtures
    LEECH,
    LEECH_2,
    data_dir,
    leeching,
    make_downloader,
    movie,
    run,
    settings,
    stream,
    tb,
)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def hold_settings(monkeypatch):
    s = settings_manager.settings.downloaders.torbox.degraded_hold
    monkeypatch.setattr(s, "enabled", True)
    monkeypatch.setattr(s, "failures", 3)
    monkeypatch.setattr(s, "window_minutes", 15)
    monkeypatch.setattr(s, "clear_minutes", 30)
    monkeypatch.setattr(s, "use_status_page", True)
    monkeypatch.setattr(s, "notify_urls", [])
    page = settings_manager.settings.downloaders.torbox.status_page
    monkeypatch.setattr(page, "url", "https://status.example")
    monkeypatch.setattr(page, "interval_minutes", 3)
    monkeypatch.setattr(page, "notify", False)
    monkeypatch.setattr(page, "notify_urls", [])
    return s


@pytest.fixture
def clocked(hold_settings):  # noqa: ARG001
    clock = Clock()
    h = TorBoxHealth(clock=clock)
    h.clock = clock
    return h


def degrade(h=health):
    for _ in range(3):
        h.record_failure("test")


# --- the degraded state ---


def test_failures_in_the_window_turn_degraded_until_quiet(clocked):
    clocked.record_failure("a")
    clocked.record_failure("b")
    assert not clocked.degraded()

    clocked.record_failure("c")
    assert clocked.degraded()
    assert "3 TorBox API call(s) failed" in clocked.reason()

    clocked.clock.now += 29 * 60
    clocked.record_failure("d")  # still failing: extends
    clocked.clock.now += 29 * 60
    assert clocked.degraded()

    clocked.clock.now += 2 * 60
    assert not clocked.degraded()


def test_spread_out_failures_dont_count(clocked):
    for _ in range(5):
        clocked.record_failure("slow drip")
        clocked.clock.now += 10 * 60

    assert not clocked.degraded()


def test_disabled_never_holds(clocked, hold_settings, monkeypatch):
    monkeypatch.setattr(hold_settings, "enabled", False)
    degrade(clocked)

    assert not clocked.degraded()
    assert not clocked.hold("something")


def test_only_torbox_files_are_held(clocked):
    degrade(clocked)

    assert clocked.holding()
    assert not clocked.holding(provider="realdebrid")


def test_status_page_alone_never_holds_but_lowers_the_bar_to_one_failure(clocked):
    clocked.set_page(PageStatus(checked_at=clocked.clock.now, problems={"API": "degraded"}))
    assert not clocked.degraded()

    clocked.record_failure("one")
    assert clocked.degraded()
    assert "status page: API degraded" in clocked.reason()


def test_status_page_lagging_on_degraded_doesnt_keep_the_hold(clocked):
    clocked.set_page(PageStatus(checked_at=clocked.clock.now, problems={"API": "degraded"}))
    clocked.record_failure("one")

    for _ in range(4):  # page keeps saying degraded, our calls work again
        clocked.clock.now += 8 * 60
        clocked.set_page(PageStatus(checked_at=clocked.clock.now, problems={"API": "degraded"}))

    assert not clocked.degraded()


def test_failure_just_before_the_page_reports_trouble_counts(clocked):
    clocked.record_failure("one")
    assert not clocked.degraded()

    clocked.set_page(PageStatus(checked_at=clocked.clock.now, problems={"Database": "downtime"}))
    assert clocked.degraded()


def test_stale_page_doesnt_lower_the_bar(clocked):
    clocked.set_page(PageStatus(checked_at=clocked.clock.now, problems={"API": "degraded"}))
    clocked.clock.now += 16 * 60  # page unreadable since

    clocked.record_failure("one")
    assert not clocked.degraded()


def test_status_page_operational_never_ends_our_own_degraded(clocked):
    degrade(clocked)
    clocked.set_page(PageStatus(checked_at=clocked.clock.now))

    assert clocked.degraded()


def test_status_page_ignored_when_turned_off(clocked, hold_settings, monkeypatch):
    monkeypatch.setattr(hold_settings, "use_status_page", False)
    clocked.set_page(PageStatus(checked_at=clocked.clock.now, problems={"API": "downtime"}))
    clocked.record_failure("one")

    assert not clocked.degraded()


def test_cdn_region_holds_only_files_on_its_servers_after_own_failures(clocked):
    cnam = "nexus-238.cnam.tb-cdn.io"
    clocked.set_page(PageStatus(checked_at=clocked.clock.now, regions={"cnam": "degraded"}))

    assert region_of(cnam) == "cnam"
    assert region_of("example.com") is None
    assert not clocked.holding(cnam)  # the page alone isn't enough

    clocked.record_cdn_failure("nexus-12.weur.tb-cdn.io")  # another region
    assert not clocked.holding(cnam)

    clocked.record_cdn_failure("nexus-5.cnam.tb-cdn.io")
    assert clocked.holding(cnam)
    assert not clocked.holding("nexus-12.weur.tb-cdn.io")
    assert not clocked.holding()
    assert not clocked.degraded()

    # Page still says degraded, but no failure here for 30 min: released
    clocked.clock.now += 31 * 60
    clocked.set_page(PageStatus(checked_at=clocked.clock.now, regions={"cnam": "degraded"}))
    assert not clocked.holding(cnam)


def test_cdn_failures_alone_dont_hold(clocked):
    clocked.record_cdn_failure("nexus-5.cnam.tb-cdn.io")

    assert not clocked.holding("nexus-5.cnam.tb-cdn.io")


def test_degraded_and_recovered_are_announced_once_with_what_was_held(
    clocked, hold_settings, monkeypatch
):
    monkeypatch.setattr(hold_settings, "notify_urls", ["discord://1/x"])
    monkeypatch.setattr(torbox_health.status_page, "run", lambda: None)
    sent = []
    monkeypatch.setattr(clocked, "_notify", sent.append)

    degrade(clocked)
    clocked.check()
    clocked.check()
    assert [n.event for n in sent] == ["TorBox Degraded"]
    assert sent[0].color == ORANGE

    assert clocked.hold("Backrooms (slow; not replaced)")
    clocked.clock.now += 31 * 60
    clocked.check()

    assert [n.event for n in sent] == ["TorBox Degraded", "TorBox Recovered"]
    assert sent[1].color == GREEN
    assert "Backrooms" in dict(sent[1].fields)["Held"]


def test_flapping_is_announced_at_most_every_30_minutes(
    clocked, hold_settings, monkeypatch
):
    monkeypatch.setattr(hold_settings, "clear_minutes", 5)
    monkeypatch.setattr(torbox_health.status_page, "run", lambda: None)
    sent = []
    monkeypatch.setattr(clocked, "_notify", sent.append)

    def flap(after_minutes):
        clocked.clock.now += after_minutes * 60
        degrade(clocked)
        clocked.check()
        clocked.clock.now += 6 * 60
        clocked.check()

    flap(0)  # announced, then recovered
    flap(1)  # within 30 min of the last "degraded" notice: quiet both ways
    flap(30)  # 37 min after it: announced again

    assert [n.event for n in sent] == [
        "TorBox Degraded",
        "TorBox Recovered",
        "TorBox Degraded",
        "TorBox Recovered",
    ]


def test_region_only_holds_dont_show_up_in_a_later_recovery(clocked, monkeypatch):
    monkeypatch.setattr(torbox_health.status_page, "run", lambda: None)
    sent = []
    monkeypatch.setattr(clocked, "_notify", sent.append)
    clocked.set_page(PageStatus(checked_at=clocked.clock.now, regions={"cnam": "degraded"}))
    clocked.record_cdn_failure("nexus-1.cnam.tb-cdn.io")
    assert clocked.hold("old region hold", host="nexus-1.cnam.tb-cdn.io")
    clocked.check()

    degrade(clocked)
    clocked.check()
    clocked.clock.now += 31 * 60
    clocked.check()

    assert "old region hold" not in dict(sent[-1].fields)["Held"]


# --- TorBox API calls feed it ---


def answer(downloader, status, body, monkeypatch, headers=None):  # noqa: F811
    response = SimpleNamespace(
        status_code=status,
        ok=status < 400,
        headers=headers or {},
        json=lambda: body,
    )
    monkeypatch.setattr(downloader.api.session, "request", lambda *_a, **_k: response)


def test_api_5xx_counts_and_429_does_not(downloader, monkeypatch):  # noqa: F811
    answer(downloader, 503, {"success": False}, monkeypatch)
    for _ in range(3):
        downloader._request("GET", "torrents/mylist")

    assert health.degraded()

    health.__init__()
    answer(downloader, 429, {"success": False}, monkeypatch, {"Retry-After": "1"})
    for _ in range(3):
        try:
            downloader._request("GET", "torrents/mylist", kind="other")
        except Exception:
            pass

    assert not health.degraded()


def test_api_timeouts_count(downloader, monkeypatch):  # noqa: F811
    import requests

    def timeout(*_a, **_k):
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(downloader.api.session, "request", timeout)
    for _ in range(3):
        with pytest.raises(requests.exceptions.Timeout):
            downloader._request("GET", "torrents/mylist")

    assert health.degraded()


def test_listing_that_says_it_failed_is_not_an_empty_account(downloader, monkeypatch):  # noqa: F811
    answer(
        downloader,
        200,
        {"success": False, "detail": "An error occurred while verifying your token."},
        monkeypatch,
    )

    with pytest.raises(Exception, match="verifying your token"):
        downloader.list_torrents()


def test_requestdl_5xx_for_a_file_still_there_counts(downloader, fake):  # noqa: F811
    existing_id = fake.add_existing(EXISTING, episode_files())
    fake.requestdl_response = httpx.Response(
        500, json={"success": False, "error": "DATABASE_ERROR", "data": None}
    )

    for _ in range(3):
        assert downloader.unrestrict_link(downloader._file_link(existing_id, 0)) is None

    assert health.degraded()


# --- what is held ---


def test_dead_link_is_not_re_sourced_while_degraded(monkeypatch):
    from program.services.filesystem.vfs import db as vfs_db

    def unrestrict(_link):
        raise DebridServiceLinkUnavailable(
            provider="torbox", link="x", reason="cdn_host_down", host="nexus-1.cnam.tb-cdn.io"
        )

    service = SimpleNamespace(key="torbox", unrestrict_link=unrestrict)
    database = vfs_db.VFSDatabase.__new__(vfs_db.VFSDatabase)
    database.downloader = SimpleNamespace(services={"torbox": service})
    entry = SimpleNamespace(
        provider="torbox",
        download_url="https://api.torbox.app/x",
        original_filename="Backrooms (2026).mp4",
        media_item=SimpleNamespace(id=886),
    )
    monkeypatch.setattr(
        vfs_db, "apply_item_mutation", lambda **_: pytest.fail("item was reset")
    )
    degrade()

    assert database._refresh_unrestricted_url(entry, session=None) == (None, False)


def test_dead_link_on_a_degraded_cdn_region_is_kept(monkeypatch):
    from program.services.filesystem.vfs import db as vfs_db

    health.set_page(
        PageStatus(checked_at=time.time(), regions={"cnam": "downtime"})
    )
    health.record_cdn_failure("nexus-9.cnam.tb-cdn.io")

    def unrestrict(_link):
        raise DebridServiceLinkUnavailable(
            provider="torbox", link="x", reason="cdn_host_down", host="nexus-1.cnam.tb-cdn.io"
        )

    database = vfs_db.VFSDatabase.__new__(vfs_db.VFSDatabase)
    database.downloader = SimpleNamespace(
        services={"torbox": SimpleNamespace(key="torbox", unrestrict_link=unrestrict)}
    )
    entry = SimpleNamespace(
        provider="torbox", download_url="x", original_filename="a.mkv", media_item=None
    )

    assert database._refresh_unrestricted_url(entry, session=None) == (None, False)


def test_failed_downloads_are_not_blacklisted_while_degraded(tb, leeching, settings, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "enabled", False)  # cached only
    item = movie([stream(f"{i}" * 40, 50) for i in range(1, 6)])  # none cached
    dl = make_downloader(tb)
    degrade()

    result = run(dl, item)

    assert item.blacklisted == []
    assert result.run_at is not None
    assert 9 * 60 <= (result.run_at - __import__("datetime").datetime.now()).total_seconds() <= 10 * 60
    # Spares TorBox: stops after 3 streams
    assert leeching.count("torrents/checkcached") <= 3


def test_no_uncached_download_is_started_while_degraded(tb, leeching, settings):  # noqa: F811
    item = movie([stream(LEECH, 40)])
    degrade()

    result = run(make_downloader(tb), item)

    assert leeching.uncached_adds == []
    assert item.blacklisted == []
    assert result.run_at is not None


def test_stalled_uncached_download_keeps_waiting_while_degraded(tb, leeching, settings, monkeypatch):  # noqa: F811
    item = movie([stream(LEECH, 40), stream(LEECH_2, 12)])
    dl = make_downloader(tb)
    run(dl, item)
    later = time.time() + 61 * 60
    monkeypatch.setattr(torbox_uncached.time, "time", lambda: later)
    degrade()

    result = run(dl, item)

    assert item.blacklisted == []
    assert leeching.uncached_adds == [LEECH]  # not given up, nothing new started
    assert result.run_at is not None
    assert tb.uncached.torrent_ids()  # still tracked, with a fresh stall clock


def test_slow_file_is_not_replaced_while_degraded(monkeypatch):
    from tests.test_slow_streams import monitor as monitor_fixture  # noqa: F401
    from program.services.streaming.slow_streams import (
        SlowFile,
        SlowStreamMonitor,
        StreamHealth,
    )

    replaced = []
    m = SlowStreamMonitor(health=StreamHealth())
    monkeypatch.setattr(m, "_replace", replaced.append)
    monkeypatch.setattr(m, "_fresh_link", lambda _f: False)
    stats = SimpleNamespace(host="nexus-152.cnam.tb-cdn.io", url="https://x")
    file = SlowFile("a.mkv", stats, [], "slow", 886, "Backrooms", None, "torbox")
    degrade()

    m._handle(file, now=0.0)

    assert replaced == []


def test_missing_torrents_are_not_treated_as_deleted_while_degraded(tmp_path, monkeypatch):
    from program.services.downloaders.torbox import TorBoxNotFound

    importer = TorBoxAccountImport()
    importer._state_file = tmp_path / "s.json"
    monkeypatch.setattr(importer, "_notify", lambda _n: None)

    def info(_tid):
        raise TorBoxNotFound("gone")

    torbox = SimpleNamespace(get_torrent_info=info)
    degrade()

    assert importer._handle_removed(torbox, {"1": {10}}, on_account={}) == set()


def test_short_listing_while_degraded_doesnt_forget_known_torrents(tmp_path, monkeypatch):
    importer = TorBoxAccountImport()
    importer._state_file = tmp_path / "s.json"
    importer._state_file.write_text('{"known": ["1", "2", "3"]}')
    listing = [{"id": 1, "name": "a", "download_finished": True}]  # 2 and 3 missing
    torbox = SimpleNamespace(list_torrents=lambda: listing, created_ids=set)
    monkeypatch.setattr(importer, "_torbox", lambda: torbox)
    monkeypatch.setattr(importer, "_referenced", dict)
    monkeypatch.setattr(importer, "_handle_removed", lambda *_: set())
    monkeypatch.setattr(importer, "_emit_pending", lambda: None)
    degrade()

    importer._run()

    assert importer._known == {"1", "2", "3"}

    # Healthy again: torrents really gone are forgotten
    health.__init__()
    importer._run()

    assert importer._known == {"1"}


def test_manual_and_unrelated_paths_are_not_held(clocked):
    # hold() is only consulted by automatic paths; holding() with no trouble is False
    assert not clocked.holding("nexus-1.cnam.tb-cdn.io")
    assert not clocked.hold("anything")


# --- status page ---

API, DB, CNAM, WEUR = "1", "2", "3", "4"


def page_data(reports=(), updates=(), api="operational", cnam="operational"):
    resources = [
        (API, "API", api),
        (DB, "Database", "operational"),
        (CNAM, "CNAM CDN", cnam),
        (WEUR, "WEUR CDN", "operational"),
    ]
    return {
        "data": {"attributes": {"aggregate_state": "operational", "logo_url": "https://logo"}},
        "included": [
            {"id": rid, "type": "status_page_resource", "attributes": {"public_name": name, "status": status}}
            for rid, name, status in resources
        ]
        + [
            {
                "id": rid,
                "type": "status_report",
                "attributes": {
                    "title": title,
                    "report_type": kind,
                    "starts_at": "2026-10-25T00:21:00.000Z",
                    "ends_at": None,
                    "aggregate_state": state,
                    "affected_resources": [{"status_page_resource_id": API, "status": "degraded"}],
                },
                "relationships": {
                    "status_updates": {"data": [{"id": u, "type": "status_update"} for u in uids]}
                },
            }
            for rid, title, kind, state, uids in reports
        ]
        + [
            {
                "id": uid,
                "type": "status_update",
                "attributes": {
                    "message": message,
                    "published_at": published,
                    "affected_resources": [{"status_page_resource_id": API, "status": status}],
                },
            }
            for uid, message, published, status in updates
        ],
    }


OLD = ("90", "Old outage", "manual", "resolved", ["900"])
OLD_U = ("900", "Fixed", "2026-10-01T00:00:00Z", "operational")
DB_OPEN = ("10", "Database Degredation", "manual", "degraded", ["100"])
U1 = ("100", "Our database is having issues", "2026-10-25T00:21:00Z", "downtime")
U2 = ("101", "Still investigating", "2026-10-25T00:40:00Z", "degraded")
U3 = ("102", "All good now", "2026-10-25T01:30:00Z", "operational")


@pytest.fixture
def page(hold_settings, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(torbox_health, "send", lambda _urls, n: sent.append(n))
    p = TorBoxStatusPage(state_file=tmp_path / "torbox_status.json")
    p.sent = sent
    return p


def notify_on(monkeypatch):
    s = settings_manager.settings.downloaders.torbox.status_page
    monkeypatch.setattr(s, "notify", True)
    monkeypatch.setattr(s, "notify_urls", ["discord://1/x"])


def test_parse_reads_problems_regions_and_open_incidents(page):
    data = page_data([OLD, DB_OPEN], [OLD_U, U1], api="degraded", cnam="downtime")

    status = page.parse(data, now=5.0)

    assert status.problems == {"API": "degraded"}
    assert status.regions == {"cnam": "downtime"}
    assert status.incidents == ["Database Degredation"]


def test_first_look_posts_only_what_is_ongoing(page, monkeypatch):
    notify_on(monkeypatch)

    page.announce(page_data([OLD, DB_OPEN], [OLD_U, U1]))

    assert [(n.event, n.title) for n in page.sent] == [("TorBox Incident", "Database Degredation")]
    notice = page.sent[0]
    assert notice.url == "https://status.example/incident/10"
    assert notice.description == "Our database is having issues"
    assert notice.color == RED  # downtime
    assert dict(notice.fields)["Affected"] == "API"
    assert notice.image == "https://logo"
    assert notice.timestamp is not None


def test_updates_and_resolution_are_posted_once_each(page, monkeypatch):
    notify_on(monkeypatch)
    page.announce(page_data([OLD, DB_OPEN], [OLD_U, U1]))

    updated = ("10", "Database Degredation", "manual", "degraded", ["100", "101"])
    page.announce(page_data([OLD, updated], [OLD_U, U1, U2]))
    page.announce(page_data([OLD, updated], [OLD_U, U1, U2]))  # nothing new

    resolved = ("10", "Database Degredation", "manual", "resolved", ["100", "101", "102"])
    page.announce(page_data([OLD, resolved], [OLD_U, U1, U2, U3]))
    page.announce(page_data([OLD, resolved], [OLD_U, U1, U2, U3]))

    events = [(n.event, n.description) for n in page.sent]
    assert events == [
        ("TorBox Incident", "Our database is having issues"),
        ("TorBox Incident Update", "Still investigating"),
        ("TorBox Incident Resolved", "All good now"),
    ]
    assert page.sent[2].color == GREEN


def test_new_incident_after_the_first_look_is_posted(page, monkeypatch):
    notify_on(monkeypatch)
    page.announce(page_data([OLD], [OLD_U]))

    page.announce(page_data([OLD, DB_OPEN], [OLD_U, U1]))

    assert [n.event for n in page.sent] == ["TorBox Incident"]


def test_resolved_without_a_new_message_still_says_so(page, monkeypatch):
    notify_on(monkeypatch)
    page.announce(page_data([DB_OPEN], [U1]))

    page.announce(page_data([("10", "Database Degredation", "manual", "resolved", ["100"])], [U1]))

    assert [n.event for n in page.sent] == ["TorBox Incident", "TorBox Incident Resolved"]


def test_notify_off_tracks_quietly_and_turning_it_on_posts_ongoing_once(page, monkeypatch):
    page.announce(page_data([OLD, DB_OPEN], [OLD_U, U1]))
    assert page.sent == []

    notify_on(monkeypatch)
    page.announce(page_data([OLD, DB_OPEN], [OLD_U, U1]))
    page.announce(page_data([OLD, DB_OPEN], [OLD_U, U1]))

    assert [n.event for n in page.sent] == ["TorBox Incident"]


def test_maintenance_is_labelled_as_such(page, monkeypatch):
    notify_on(monkeypatch)
    page.announce(page_data([], []))

    page.announce(page_data([("20", "API Maintenance", "maintenance", "maintenance", ["200"])],
                            [("200", "Upgrading", "2026-10-26T06:00:00Z", "maintenance")]))

    assert [n.event for n in page.sent] == ["TorBox Maintenance"]


def test_run_feeds_health_and_survives_an_unreadable_page(hold_settings, tmp_path):
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: page_data([DB_OPEN], [U1], api="degraded"),
    )
    p = TorBoxStatusPage(get=lambda _url: response, state_file=tmp_path / "s.json")

    p.run()
    assert not health.degraded()  # the page alone doesn't decide
    health.record_failure("one")
    assert health.degraded()

    def broken(_url):
        raise httpx.ConnectError("down")

    health.__init__()
    p = TorBoxStatusPage(get=broken, state_file=tmp_path / "s2.json")
    with pytest.raises(httpx.ConnectError):
        p.run()  # health.check() catches this; health stays as it was
    assert not health.degraded()


def test_page_is_not_read_when_nothing_uses_it(hold_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(hold_settings, "use_status_page", False)
    calls = []
    p = TorBoxStatusPage(get=lambda url: calls.append(url), state_file=tmp_path / "s.json")

    p.run()

    assert calls == []
