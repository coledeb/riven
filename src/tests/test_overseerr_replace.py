"""Overseerr issues from users replace the reported item, then get resolved."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from kink import di

from program.apis.overseerr_api import OverseerrAPI
from program.services.content.overseerr_replace import OverseerrReplace

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def issue(id, created=NOW + timedelta(minutes=1), user=5, season=2, episode=3):
    return {
        "id": id,
        "createdAt": created.isoformat().replace("+00:00", "Z"),
        "createdBy": {"id": user, "displayName": "Viewer"},
        "problemSeason": season,
        "problemEpisode": episode,
        "media": {"mediaType": "tv", "tmdbId": 4586, "tvdbId": 76568},
        "comments": [{"message": "audio out of sync"}],
    }


class FakeOverseerr:
    def __init__(self):
        self.issues = []
        self.comments = []
        self.resolved = []

    def open_issues(self):
        return list(reversed(self.issues))  # newest first, like Seerr

    def comment_issue(self, issue_id, message, user_id=0):
        if issue_id not in {i["id"] for i in self.issues}:
            return False
        self.comments.append((issue_id, message))
        return True

    def resolve_issue(self, issue_id, user_id=0):
        self.resolved.append(issue_id)
        self.issues = [i for i in self.issues if i["id"] != issue_id]
        return True


@pytest.fixture
def seerr():
    previous = di[OverseerrAPI] if OverseerrAPI in di else None
    fake = FakeOverseerr()
    di[OverseerrAPI] = fake
    yield fake
    if previous is not None:
        di[OverseerrAPI] = previous


@pytest.fixture
def service(tmp_path, monkeypatch):
    s = OverseerrReplace()
    s._state_file = tmp_path / "overseerr_replace.json"
    s._own_issues_file = tmp_path / "overseerr_issues.json"
    s.found = {}  # issue id -> (item id, reason)
    s.progress = {}  # item id -> outcome
    s.replace_calls = []

    def find(issue):
        if not issue["problemSeason"]:
            return None, "whole show"
        return s.found.get(issue["id"], (77, None))

    def replace(item_id, issue_id):
        s.replace_calls.append((item_id, issue_id))
        return f"item {item_id}", object()

    monkeypatch.setattr(s, "_find", find)
    monkeypatch.setattr(s, "_replace", replace)
    monkeypatch.setattr(s, "_progress", lambda item_id: s.progress.get(item_id, ("waiting", "")))
    monkeypatch.setattr(s, "_notify", lambda *a: None)
    monkeypatch.setattr(s.settings, "user_id", 17)
    monkeypatch.setattr(s.settings, "cooldown_hours", 6)
    s._run(NOW)  # first run: remembers when it was turned on
    return s


def test_first_run_ignores_issues_already_open(tmp_path, seerr, monkeypatch):
    s = OverseerrReplace()
    s._state_file = tmp_path / "state.json"
    s._own_issues_file = tmp_path / "own.json"
    seerr.issues = [issue(1, created=NOW - timedelta(days=1))]
    monkeypatch.setattr(s, "_replace", lambda *a: pytest.fail("replaced"))

    s._run(NOW)
    s._run(NOW + timedelta(minutes=2))

    assert seerr.comments == []


def test_episode_report_replaces_then_resolves(service, seerr):
    seerr.issues = [issue(10)]
    service._run(NOW + timedelta(minutes=2))

    assert service.replace_calls == [(77, 10)]
    assert seerr.comments[0][1].startswith("Finding another copy of item 77")

    service._run(NOW + timedelta(minutes=3))
    assert seerr.resolved == []

    service.progress[77] = ("done", "Show.S02.1080p")
    service._run(NOW + timedelta(minutes=4))

    assert seerr.resolved == [10]
    assert seerr.comments[-1] == (10, "New copy is in: Show.S02.1080p.")
    assert service.replace_calls == [(77, 10)]


def test_whole_show_report_asks_instead(service, seerr):
    seerr.issues = [issue(11, season=0, episode=0)]
    service._run(NOW + timedelta(minutes=2))
    service._run(NOW + timedelta(minutes=3))

    assert service.replace_calls == []
    assert len(seerr.comments) == 1
    assert "not a whole show" in seerr.comments[0][1]


def test_own_and_old_issues_are_ignored(service, seerr):
    service._own_issues_file.write_text(json.dumps({"open": {"season:4": {"id": 12}}}))
    seerr.issues = [
        issue(12),
        issue(13, user=17),
        issue(14, created=NOW - timedelta(minutes=1)),
    ]
    service._run(NOW + timedelta(minutes=2))

    assert service.replace_calls == []
    assert seerr.comments == []


def test_second_report_follows_running_replace(service, seerr):
    seerr.issues = [issue(20)]
    service._run(NOW + timedelta(minutes=2))
    seerr.issues.append(issue(21, created=NOW + timedelta(minutes=3)))
    service._run(NOW + timedelta(minutes=4))

    assert service.replace_calls == [(77, 20)]
    assert "Already finding another copy (issue #20)" in seerr.comments[-1][1]

    service.progress[77] = ("done", "")
    service._run(NOW + timedelta(minutes=5))
    assert sorted(seerr.resolved) == [20, 21]


def test_cooldown_leaves_issue_open(service, seerr):
    seerr.issues = [issue(30)]
    service._run(NOW + timedelta(minutes=2))
    service.progress[77] = ("done", "")
    service._run(NOW + timedelta(minutes=3))

    seerr.issues = [issue(31, created=NOW + timedelta(hours=1))]
    service._run(NOW + timedelta(hours=1, minutes=1))

    assert service.replace_calls == [(77, 30)]
    assert "won't replace it again within 6 hours" in seerr.comments[-1][1]
    assert 31 not in seerr.resolved

    seerr.issues = [issue(32, created=NOW + timedelta(hours=7))]
    service._run(NOW + timedelta(hours=7, minutes=1))
    assert service.replace_calls == [(77, 30), (77, 32)]


def test_failed_replace_stays_open(service, seerr):
    seerr.issues = [issue(40)]
    service._run(NOW + timedelta(minutes=2))
    service.progress[77] = ("failed", "")
    service._run(NOW + timedelta(minutes=3))
    service._run(NOW + timedelta(minutes=4))

    assert seerr.resolved == []
    assert [m for _, m in seerr.comments].count(
        "Riven couldn't find another copy. Leaving this open for the admin."
    ) == 1


def test_issue_closed_meanwhile_is_dropped(service, seerr):
    seerr.issues = [issue(50)]
    service._run(NOW + timedelta(minutes=2))
    seerr.issues = []
    service.progress[77] = ("done", "")
    service._run(NOW + timedelta(minutes=3))

    assert seerr.resolved == []
    assert json.loads(service._state_file.read_text())["handled"] == {}
