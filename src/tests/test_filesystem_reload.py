"""A settings reload must keep the mounted RivenVFS instead of mounting a second one."""

import os

import pytest

import program.services.filesystem.vfs as vfs_module
from program.services.filesystem.filesystem_service import FilesystemService
from program.settings import settings_manager


class FakeVFS:
    created = 0

    def __init__(self, mountpoint, downloader):
        FakeVFS.created += 1
        self.mountpoint = os.path.abspath(mountpoint)
        self.downloader = downloader
        self.mounted = True
        self.closed = False
        self.synced = 0

    def set_downloader(self, downloader):
        self.downloader = downloader

    def sync(self):
        self.synced += 1

    def close(self):
        self.closed = True
        self.mounted = False


@pytest.fixture
def fake_vfs(monkeypatch, tmp_path):
    FakeVFS.created = 0
    monkeypatch.setattr(vfs_module, "RivenVFS", FakeVFS)
    monkeypatch.setattr(
        settings_manager.settings.filesystem, "mount_path", tmp_path / "mount"
    )
    return tmp_path


@pytest.mark.usefixtures("fake_vfs")
def test_reload_reuses_mounted_vfs():
    first = FilesystemService("downloader-1")
    second = FilesystemService("downloader-2", first)

    assert FakeVFS.created == 1
    assert second.riven_vfs is not None
    assert second.riven_vfs.downloader == "downloader-2"
    assert second.riven_vfs.synced == 1
    assert not second.riven_vfs.closed
    assert first.riven_vfs is second.riven_vfs
    assert second.validate()


def test_reload_remounts_when_mount_path_changes(fake_vfs, monkeypatch):
    first = FilesystemService("downloader-1")
    old_vfs = first.riven_vfs

    monkeypatch.setattr(
        settings_manager.settings.filesystem, "mount_path", fake_vfs / "other"
    )
    second = FilesystemService("downloader-2", first)

    assert old_vfs.closed
    assert FakeVFS.created == 2
    assert second.riven_vfs.mountpoint == str(fake_vfs / "other")


@pytest.mark.usefixtures("fake_vfs")
def test_reload_replaces_dead_mount():
    first = FilesystemService("downloader-1")
    first.riven_vfs.mounted = False

    second = FilesystemService("downloader-2", first)

    assert FakeVFS.created == 2
    assert second.riven_vfs.mounted


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args):
        return self

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, entries, items):
        self.entries, self.items = entries, items

    def query(self, model):
        return FakeQuery(self.entries if model.__name__ == "MediaEntry" else self.items)

    def commit(self):
        pass


def _bare_vfs(monkeypatch, entries=(), items=()):
    import contextlib
    import threading

    import program.services.filesystem.vfs.rivenvfs as rivenvfs_module
    import program.services.library_profile_matcher as matcher_module

    sessions = []

    @contextlib.contextmanager
    def fake_db_session():
        sessions.append(1)
        yield FakeSession(list(entries), list(items))

    class FakeMatcher:
        def get_matching_profiles(self, item):
            return []

    monkeypatch.setattr(rivenvfs_module, "db_session", fake_db_session)
    monkeypatch.setattr(matcher_module, "LibraryProfileMatcher", FakeMatcher)

    vfs = object.__new__(rivenvfs_module.RivenVFS)
    vfs._tree_lock = threading.RLock()
    vfs._root = None
    vfs._inode_to_node = {}
    vfs._pending_invalidations = set()
    vfs._last_profile_hash = None
    vfs._ensure_library_profile_directories = lambda: None
    return vfs, sessions


def test_full_sync_skips_when_list_valued_profiles_unchanged(monkeypatch):
    from program.settings.models import LibraryProfile, LibraryProfileFilterRules

    profile = LibraryProfile(
        name="Kids",
        library_path="/kids",
        enabled=False,
        filter_rules=LibraryProfileFilterRules(content_ratings=["G", "PG"]),
    )
    monkeypatch.setattr(
        settings_manager.settings.filesystem, "library_profiles", {"kids": profile}
    )
    vfs, sessions = _bare_vfs(monkeypatch)

    vfs._sync_full()
    assert vfs._last_profile_hash is not None
    rebuilt = len(sessions)

    vfs._sync_full()
    assert len(sessions) == rebuilt  # unchanged profiles: no rebuild

    profile.enabled = True
    vfs._sync_full()
    assert len(sessions) > rebuilt  # any profile change still rebuilds


def test_full_sync_holds_tree_lock_while_rebuilding(monkeypatch):
    item = type("Item", (), {"id": 7})()
    entry = type("Entry", (), {"id": 1, "media_item": item, "library_profiles": []})()
    vfs, _ = _bare_vfs(monkeypatch, entries=[entry], items=[item])
    monkeypatch.setattr(settings_manager.settings.filesystem, "library_profiles", {})
    owned = []
    vfs.add = lambda it: owned.append(vfs._tree_lock._is_owned()) or True

    vfs._sync_full()

    assert owned == [True]
