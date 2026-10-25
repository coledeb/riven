"""A movie or show is announced as completed once, not again after a re-source."""

import json
from types import SimpleNamespace

import pytest

from program.services import notifications
from program.services.notifications import CompletionLog


@pytest.fixture
def sizes(monkeypatch):
    sizes = {}
    monkeypatch.setattr(notifications, "_completed_size", lambda item: sizes[item.id])
    monkeypatch.setattr(notifications, "_already_completed", lambda: {"7": 1})
    return sizes


def item(item_id):
    return SimpleNamespace(id=item_id, log_string=f"item {item_id}")


def test_re_completed_movie_is_not_announced_again(tmp_path, sizes):
    log = CompletionLog(tmp_path / "announced.json")
    sizes[1] = 1

    assert log.first(item(1))
    assert not log.first(item(1))  # re-sourced: Completed again


def test_show_is_announced_again_only_with_more_episodes(tmp_path, sizes):
    log = CompletionLog(tmp_path / "announced.json")
    sizes[2] = 22

    assert log.first(item(2))
    sizes[2] = 21  # one episode being re-sourced
    assert not log.first(item(2))
    sizes[2] = 22
    assert not log.first(item(2))
    sizes[2] = 44  # a new season finished
    assert log.first(item(2))


def test_what_was_completed_before_the_log_isnt_announced(tmp_path, sizes):
    path = tmp_path / "announced.json"
    sizes[7] = 1

    assert not CompletionLog(path).first(item(7))
    assert json.loads(path.read_text()) == {"7": 1}


def test_log_survives_a_restart(tmp_path, sizes):
    path = tmp_path / "announced.json"
    sizes[3] = 1
    assert CompletionLog(path).first(item(3))

    assert not CompletionLog(path).first(item(3))


def test_size_that_cant_be_counted_still_announces_once(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "_already_completed", lambda: {})

    def broken(_item):
        raise RuntimeError("detached")

    monkeypatch.setattr(notifications, "_completed_size", broken)
    log = CompletionLog(tmp_path / "announced.json")

    assert log.first(item(4))
    assert not log.first(item(4))
