"""Overseerr issues for missing items: open after a delay, comment, resolve, survive restarts."""

from datetime import datetime, timedelta

import pytest
from kink import di

from program.apis.overseerr_api import OverseerrAPI
from program.services.content.overseerr_issues import Gap, OverseerrIssues

NOW = datetime(2026, 10, 18, 12, 0)


class FakeOverseerr:
    def __init__(self):
        self.media = {("movie", "278"): 24, ("tvdb", "410743"): 26}
        self.created = []
        self.comments = []
        self.resolved = []
        self.deleted = set()
        self.next_id = 100

    def media_ids(self):
        return self.media

    def create_issue(self, media_id, message, season=0, episode=0, user_id=0):
        self.next_id += 1
        self.created.append((self.next_id, media_id, season, episode, user_id, message))
        return self.next_id

    def comment_issue(self, issue_id, message, user_id=0):
        if issue_id in self.deleted:
            return False
        self.comments.append((issue_id, message))
        return True

    def resolve_issue(self, issue_id, user_id=0):
        if issue_id in self.deleted:
            return False
        self.resolved.append(issue_id)
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
def issues(tmp_path, monkeypatch):
    service = OverseerrIssues()
    service._state_file = tmp_path / "overseerr_issues.json"
    service.gaps = {}
    service.notes = {}
    monkeypatch.setattr(service, "_gaps", lambda now: dict(service.gaps))
    monkeypatch.setattr(
        service, "_closing_note", lambda key: service.notes.get(key, "Fixed: available again.")
    )
    settings = service.settings
    monkeypatch.setattr(settings, "after_minutes", 30)
    monkeypatch.setattr(settings, "user_id", 17)
    return service


def movie():
    gap = Gap(5, "The Shawshank Redemption (1994)", "movie", "278", None)
    gap.states.add("Scraped")
    return gap


def season(*episodes):
    gap = Gap(40, "Reasonable Doubt season 4", "tv", None, "410743", season=4)
    gap.episodes.extend(episodes)
    gap.states.add("Indexed")
    return gap


def put(service, *gaps):
    service.gaps = {g.key: g for g in gaps}


def test_issue_opens_only_after_delay(issues, seerr):
    put(issues, movie())
    issues._run(NOW)
    issues._run(NOW + timedelta(minutes=29))
    assert seerr.created == []

    issues._run(NOW + timedelta(minutes=30))
    assert len(seerr.created) == 1
    issue_id, media_id, s, e, user, message = seerr.created[0]
    assert (media_id, s, e, user) == (24, 0, 0, 17)
    assert "missing since 2026-10-18 12:00" in message

    issues._run(NOW + timedelta(minutes=60))
    assert len(seerr.created) == 1


def test_clock_resets_when_item_recovers(issues, seerr):
    put(issues, movie())
    issues._run(NOW)
    put(issues)
    issues._run(NOW + timedelta(minutes=20))
    put(issues, movie())
    issues._run(NOW + timedelta(minutes=40))
    assert seerr.created == []


def test_clock_survives_restart(issues, seerr, monkeypatch):
    put(issues, movie())
    issues._run(NOW)

    fresh = OverseerrIssues()
    fresh._state_file = issues._state_file
    monkeypatch.setattr(fresh, "_gaps", lambda now: {movie().key: movie()})
    fresh._run(NOW + timedelta(minutes=31))
    assert len(seerr.created) == 1


def test_season_issue_by_tvdb_with_comment_and_resolve(issues, seerr):
    put(issues, season(3))
    issues._run(NOW)
    issues._run(NOW + timedelta(minutes=31))
    issue_id, media_id, s, e, _, message = seerr.created[0]
    assert (media_id, s, e) == (26, 4, 3)
    assert message.startswith("S04E03 has no working file")

    put(issues, season(3, 4))
    issues._run(NOW + timedelta(minutes=40))
    assert seerr.comments == [(issue_id, "Also missing now: S04E04.")]

    put(issues, season(4))  # E03 fixed, E04 still missing: stays open
    issues._run(NOW + timedelta(minutes=45))
    assert seerr.resolved == []

    put(issues)
    issues._run(NOW + timedelta(minutes=50))
    assert seerr.comments[-1] == (issue_id, "Fixed: available again.")
    assert seerr.resolved == [issue_id]
    assert len(seerr.created) == 1


def test_whole_season_issue_has_no_episode(issues, seerr):
    put(issues, season(1, 2, 3))
    issues._run(NOW)
    issues._run(NOW + timedelta(minutes=31))
    _, _, s, e, _, message = seerr.created[0]
    assert (s, e) == (4, 0)
    assert "S04E01, S04E02, S04E03 have" in message


def test_not_in_overseerr_is_skipped(issues, seerr):
    seerr.media = {}
    put(issues, movie())
    issues._run(NOW)
    issues._run(NOW + timedelta(minutes=31))
    assert seerr.created == []


def test_issue_deleted_in_overseerr(issues, seerr):
    put(issues, movie())
    issues._run(NOW)
    issues._run(NOW + timedelta(minutes=31))
    seerr.deleted.add(seerr.created[0][0])

    put(issues)
    issues._run(NOW + timedelta(minutes=40))
    assert seerr.resolved == []
    assert issues._load()["open"] == {}


def test_removed_item_closes_with_note(issues, seerr):
    put(issues, movie())
    issues._run(NOW)
    issues._run(NOW + timedelta(minutes=31))
    issues.notes[movie().key] = "Removed from Riven, closing."
    put(issues)
    issues._run(NOW + timedelta(minutes=40))
    assert seerr.comments[-1][1] == "Removed from Riven, closing."
    assert len(seerr.resolved) == 1
