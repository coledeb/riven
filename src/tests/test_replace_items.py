"""POST /items/replace blacklists the current source and re-sources, keeping the file."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import trio
from fastapi import HTTPException

from routers.models.shared import IdListPayload
from routers.secure import items


class FakeItem:
    def __init__(self, id):
        self.id = id
        self.log_string = f"item {id}"
        self.calls = []

    def blacklist_active_stream(self, reason):
        self.calls.append(("blacklist", reason))

    def reset(self, keep_vfs=False):
        self.calls.append(("reset", keep_vfs))


def setup(monkeypatch, stored):
    events = []

    @contextmanager
    def session():
        yield SimpleNamespace(get=lambda _cls, id: stored.get(id), commit=lambda: None)

    program = SimpleNamespace(
        em=SimpleNamespace(add_event=events.append), services=None
    )
    monkeypatch.setattr(items, "db_session", session)
    monkeypatch.setattr(items, "di", {items.Program: program})
    monkeypatch.setattr(
        items,
        "apply_item_mutation",
        lambda program, session, item, mutation_fn, bubble_parents: mutation_fn(
            item, session
        ),
    )
    return events


def test_replace_blacklists_then_resets_keeping_the_file(monkeypatch):
    movie = FakeItem(7)
    events = setup(monkeypatch, {7: movie})

    response = trio.run(items.replace_items, IdListPayload(ids=["7"]))

    assert response.ids == [7]
    assert movie.calls == [
        ("blacklist", items.BlacklistReason.MANUAL),
        ("reset", True),
    ]
    assert [e.item_id for e in events] == [7]


def test_replace_unknown_item_is_a_404(monkeypatch):
    setup(monkeypatch, {})

    with pytest.raises(HTTPException) as excinfo:
        trio.run(items.replace_items, IdListPayload(ids=["999"]))

    assert excinfo.value.status_code == 404


class FakePlexSession:
    def __init__(self, log):
        self.log = log

    def stop(self, reason):
        self.log.append(("stop", reason))


def test_replace_stops_plex_playback_first(monkeypatch):
    from program.services.updaters.plex import PlexUpdater

    movie = FakeItem(7)
    setup(monkeypatch, {7: movie})
    plex = SimpleNamespace(
        initialized=True,
        sessions_playing=lambda paths: [FakePlexSession(movie.calls)]
        if paths == ["/mnt/lib/movies/M (2000)/M.mkv"]
        else [],
    )
    items.di[items.Program].services = SimpleNamespace(
        filesystem=SimpleNamespace(
            riven_vfs=SimpleNamespace(
                video_paths=lambda item: ["/movies/M (2000)/M.mkv"]
            )
        ),
        updater=SimpleNamespace(
            library_path="/mnt/lib", services={PlexUpdater: plex}
        ),
    )

    trio.run(items.replace_items, IdListPayload(ids=["7"]))

    assert movie.calls == [
        ("stop", items.REPLACE_STOP_MESSAGE),
        ("blacklist", items.BlacklistReason.MANUAL),
        ("reset", True),
    ]
