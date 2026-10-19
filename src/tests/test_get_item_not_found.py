"""A missing item must answer 404, not be logged and turned into a 500."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import trio
from fastapi import HTTPException

from routers.secure import items


@contextmanager
def empty_session():
    result = SimpleNamespace(
        unique=lambda: SimpleNamespace(scalar_one_or_none=lambda: None)
    )
    yield SimpleNamespace(execute=lambda _query: result)


def test_missing_item_is_a_404(monkeypatch):
    monkeypatch.setattr(items, "db_session", empty_session)

    with pytest.raises(HTTPException) as excinfo:
        trio.run(items.get_item, "999999", "item")

    assert excinfo.value.status_code == 404
