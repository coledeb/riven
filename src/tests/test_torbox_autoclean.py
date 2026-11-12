from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from program.services.downloaders import torbox_autoclean
from program.services.downloaders.torbox_autoclean import AutoClean, last_slot
from program.settings import settings_manager
from program.settings.models import TorBoxAutoCleanModel

TZ = timezone(timedelta(hours=-6))


def at(day, hour, minute=0):
    # 2026-11-02 is a Monday
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def test_time_must_be_half_hour_steps():
    assert TorBoxAutoCleanModel(time="4:30").time == "04:30"

    for bad in ("04:15", "24:00", "4", "noon"):
        with pytest.raises(ValidationError):
            TorBoxAutoCleanModel(time=bad)


def test_last_slot_daily_and_weekly():
    assert last_slot(at(3, 5), "daily", "04:30", "sunday") == at(3, 4, 30)
    assert last_slot(at(3, 4), "daily", "04:30", "sunday") == at(2, 4, 30)
    assert last_slot(at(3, 5), "off", "04:30", "sunday") is None
    # Tuesday 05:00 → Monday 04:00; Monday 03:00 → previous Monday
    assert last_slot(at(3, 5), "weekly", "04:00", "monday") == at(2, 4)
    assert last_slot(at(2, 3), "weekly", "04:00", "monday") == at(2, 4) - timedelta(days=7)


class FakeTorBox:
    def __init__(self, account, used=()):
        self.account = account
        self.used = {str(u) for u in used}
        self.deleted = []
        self.uncached = type("U", (), {"torrent_ids": lambda self: {"9"}})()

    def _iter_account(self):
        return iter(self.account)

    def _used_ids(self):
        return set(self.used)

    def delete_torrent(self, tid, include_foreign=False, cleanup=False):
        assert include_foreign and cleanup
        if str(tid) in self.used:
            return False
        self.deleted.append(str(tid))
        return True


@pytest.fixture
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(torbox_autoclean, "data_dir_path", tmp_path)
    s = settings_manager.settings.downloaders.torbox.autoclean
    old = s.model_dump()
    yield s
    for k, v in old.items():
        setattr(s, k, v)


def torrent(tid, age_hours, now):
    created = (now - timedelta(hours=age_hours)).astimezone(timezone.utc)
    return {
        "id": tid,
        "hash": f"{tid:040d}",
        "name": f"T{tid}",
        "size": 1e9,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def test_due_arms_on_enable_and_runs_next_slot_once(clean):
    clean.schedule, clean.time = "daily", "04:00"
    ac = AutoClean(FakeTorBox([]))

    assert ac.due(at(3, 5)) is None  # turned on after today's slot: arm only
    assert ac.due(at(3, 6)) is None
    slot = ac.due(at(4, 4, 1))
    assert slot == at(4, 4)
    ac._state["last_run"] = at(4, 4, 1).isoformat()
    assert ac.due(at(4, 5)) is None
    # Missed by more than the catch-up window: skipped
    assert ac.due(at(5, 4) + torbox_autoclean.CATCH_UP + timedelta(minutes=1)) is None


def test_changing_time_or_turning_off_rearms(clean):
    clean.schedule, clean.time = "daily", "04:00"
    ac = AutoClean(FakeTorBox([]))
    ac.due(at(3, 3))
    clean.time = "02:00"
    assert ac.due(at(3, 3)) is None  # moved to a slot already past: re-armed, not run
    clean.schedule = "off"
    assert ac.due(at(3, 3)) is None and "armed" not in ac._state


def test_run_deletes_only_unused_old_torrents(clean, tmp_path):
    now = datetime.now().astimezone()
    clean.min_age_hours = 24
    account = [
        torrent(1, 48, now),  # unused, old → deleted
        torrent(2, 48, now),  # used by a file
        torrent(3, 2, now),  # too new
        torrent(9, 48, now),  # uncached download still running
    ]
    fake = FakeTorBox(account, used=[2])
    deleted = AutoClean(fake).run()

    assert fake.deleted == ["1"]
    assert [t["id"] for t in deleted] == [1]
    backups = list((tmp_path / "torbox_autoclean").glob("deleted-*.json"))
    assert len(backups) == 1 and '"T1"' in backups[0].read_text()


def test_tick_records_last_run(clean, monkeypatch):
    import time

    monkeypatch.setenv("TZ", "America/Chicago")  # at() is CST
    time.tzset()
    clean.schedule, clean.time = "daily", "04:00"
    ac = AutoClean(FakeTorBox([]))
    ac._state.update(armed=at(1, 0).isoformat(), schedule="daily 04:00")
    monkeypatch.setattr(
        torbox_autoclean, "datetime", type("D", (datetime,), {"now": staticmethod(lambda: at(3, 4, 1))})
    )
    try:
        ac.tick()
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert ac._state["last_run"] == at(3, 4, 1).isoformat()
