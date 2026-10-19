"""A manual retry/scrape must not be deduped away by an event queued for later."""

from contextlib import nullcontext
from datetime import datetime, timedelta

import pytest

from program.managers import event_manager as event_manager_module
from program.managers.event_manager import EventManager
from program.types import Event

SEASON, EPISODES = 10, [11, 12]


@pytest.fixture
def em(monkeypatch):
    monkeypatch.setattr(event_manager_module, "db_session", nullcontext)
    monkeypatch.setattr(
        event_manager_module.db_functions,
        "get_item_ids",
        lambda _session, item_id: (item_id, EPISODES if item_id == SEASON else []),
    )

    manager = EventManager()
    manager.add_event_to_queue = lambda event: manager._queued_events.append(event)
    return manager


def later(item_id):
    return Event("Downloader", item_id, run_at=datetime.now() + timedelta(hours=1))


def test_add_event_is_blocked_by_a_queued_child(em):
    em._queued_events += [later(11), later(11)]

    assert not em.add_event(Event("API", SEASON))


def test_requeue_event_replaces_queued_events_for_the_item_and_children(em):
    em._queued_events += [later(SEASON), later(11), later(11), later(99)]

    assert em.requeue_event(Event("API", SEASON))
    assert [(e.emitted_by, e.item_id) for e in em._queued_events] == [
        ("Downloader", 99),
        ("API", SEASON),
    ]


def test_requeue_event_leaves_running_events_alone(em):
    em._running_events.append(Event("Scraping", 12))

    assert not em.requeue_event(Event("API", SEASON))
    assert em._running_events
