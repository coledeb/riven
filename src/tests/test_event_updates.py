"""event_update reports which service is working on each item."""

import threading
from concurrent.futures import Future

from program.managers.event_manager import EventManager, FutureWithEvent
from program.types import Event


def running(service_name, event):
    return FutureWithEvent(Future(), event, threading.Event(), service_name)


def test_jobs_are_listed_under_the_service_running_them():
    em = EventManager()
    em._futures += [
        running("Downloader", Event("Scraping", 1)),
        running("Scraping", Event("API", 2)),
        running("FilesystemService", Event("Downloader", 3)),
        running("Updater", Event("Downloader")),
    ]

    updates = em.get_event_updates()

    assert updates["Downloader"] == [1]
    assert updates["Scraping"] == [2]
    assert updates["Filesystem"] == [3]
    assert updates["Updater"] == []
    assert set(updates) == {
        "Indexer",
        "Scraping",
        "Downloader",
        "Filesystem",
        "Updater",
        "PostProcessing",
    }
