"""File times are stored as naive UTC and must not shift by the local UTC offset."""

import os
import time
from datetime import datetime, timezone

import pytest

from program.services.filesystem.vfs.rivenvfs import RivenVFS

UTC_7AM_NS = int(datetime(2026, 10, 19, 7, tzinfo=timezone.utc).timestamp()) * 10**9


@pytest.fixture
def chicago_time():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.mark.usefixtures("chicago_time")
@pytest.mark.parametrize(
    "stored",
    ["2026-10-19T07:00:00", "2026-10-19T07:00:00+00:00", "2026-10-19T02:00:00-05:00"],
)
def test_stored_times_are_read_as_utc(stored):
    assert RivenVFS._timestamp_ns(stored) == UTC_7AM_NS
