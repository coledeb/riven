"""A failing stream fails every pending read; its error is logged once per file
handle, with repeats counted (a minute later, or when the file is closed)."""

from types import SimpleNamespace

import pytest
from loguru import logger

from program.services.filesystem.vfs import rivenvfs as rivenvfs_module


@pytest.fixture
def vfs():
    vfs = object.__new__(rivenvfs_module.RivenVFS)
    vfs._read_errors = {}
    return vfs


@pytest.fixture
def errors():
    messages = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="ERROR")
    yield messages
    logger.remove(sink)


STREAM = SimpleNamespace(build_log_message=lambda m: f"{m} [fh: 7 | file=a.mkv]")


def test_repeats_are_counted_and_reported_on_close(vfs, errors):
    for _ in range(495):
        vfs._log_read_error(7, STREAM, RuntimeError("boom"))

    assert errors == ["RuntimeError: boom [fh: 7 | file=a.mkv]"]

    vfs._flush_read_errors(7)

    assert errors[1] == "RuntimeError: boom [fh: 7 | file=a.mkv] (repeated 494 more times)"
    assert vfs._read_errors == {}


def test_same_error_is_logged_again_after_the_interval(vfs, errors, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rivenvfs_module.time, "monotonic", lambda: now[0])

    vfs._log_read_error(7, STREAM, RuntimeError("boom"))
    vfs._log_read_error(7, STREAM, RuntimeError("boom"))
    now[0] += rivenvfs_module.READ_ERROR_LOG_INTERVAL
    vfs._log_read_error(7, STREAM, RuntimeError("boom"))

    assert errors[-1].endswith("(repeated 1 more times)")
    assert len(errors) == 2


def test_other_errors_and_handles_are_logged_separately(vfs, errors):
    vfs._log_read_error(7, STREAM, RuntimeError("boom"))
    vfs._log_read_error(7, STREAM, ValueError("other"))
    vfs._log_read_error(8, None, RuntimeError("boom"))

    assert len(errors) == 3
    assert errors[2] == "RuntimeError: boom"
