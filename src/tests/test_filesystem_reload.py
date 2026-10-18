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
