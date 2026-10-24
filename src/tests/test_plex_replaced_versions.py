import threading
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from program.services.updaters import plex
from program.services.updaters.plex import (
    PlexUpdater,
    library_file,
    replaced_versions,
)

MAPPINGS = {"/mnt/debrid/riven": "\\\\wsl-media\\riven"}
LIBRARY = "/mnt/debrid/riven"
SHARE = "\\\\wsl-media\\riven\\shows\\Abbott Elementary (2021) {tvdb-402910}\\Season 01\\"
NEW = SHARE + "Abbott Elementary - s01e12.mp4"
OLD = SHARE + "Abbott Elementary - s01e12.mkv"
NEW_LOCAL = (
    "/mnt/debrid/riven/shows/Abbott Elementary (2021) {tvdb-402910}/Season 01/"
    "Abbott Elementary - s01e12.mp4"
)
OLD_LOCAL = NEW_LOCAL[:-4] + ".mkv"


def video(*media: str) -> ET.Element:
    return ET.fromstring(
        '<Video ratingKey="1610" type="episode" title="Ava vs. Superintendent" '
        'grandparentTitle="Abbott Elementary" parentIndex="1" index="12">'
        + "".join(media)
        + "</Video>"
    )


def present(file: str = NEW, media_id: str = "2049") -> str:
    return (
        f'<Media id="{media_id}"><Part accessible="1" exists="1" file="{file}"/></Media>'
    )


def gone(file: str = OLD, media_id: str = "1937", exists: str = "0") -> str:
    return (
        f'<Media id="{media_id}" deletedAt="1792753027">'
        f'<Part accessible="{exists}" exists="{exists}" file="{file}"/></Media>'
    )


def riven_path(path: str) -> str | None:
    return library_file(path, MAPPINGS, LIBRARY)


def wanted(*files: str) -> set[str]:
    return {f.lower() for f in files}


def only(*paths: str):
    return lambda path: path in paths


def test_library_file_reverses_the_mapping():
    assert riven_path(NEW) == NEW_LOCAL
    assert riven_path(NEW.upper().replace("\\\\WSL-MEDIA\\RIVEN", "\\\\wsl-media\\riven"))
    assert riven_path("D:\\Plex\\TV Shows\\X\\x.mkv") is None
    assert riven_path("\\\\wsl-media\\rivenx\\a.mkv") is None
    assert library_file("/mnt/debrid/riven/movies/a.mkv", {}, LIBRARY) == (
        "/mnt/debrid/riven/movies/a.mkv"
    )
    assert library_file("/elsewhere/a.mkv", {}, LIBRARY) is None


def test_replaced_version_is_found():
    result = replaced_versions(
        video(present(), gone()), wanted(NEW), riven_path, only(NEW_LOCAL)
    )
    assert result == [("1937", [OLD])]


def test_old_file_still_in_riven_is_kept():
    assert (
        replaced_versions(
            video(present(), gone()),
            wanted(NEW),
            riven_path,
            only(NEW_LOCAL, OLD_LOCAL),
        )
        == []
    )


def test_item_riven_did_not_just_update_is_left_alone():
    assert (
        replaced_versions(
            video(present(), gone()), wanted(), riven_path, only(NEW_LOCAL)
        )
        == []
    )


def test_nothing_removed_when_new_file_is_missing_in_riven():
    # VFS down: Riven can't see the new file either.
    assert (
        replaced_versions(video(present(), gone()), wanted(NEW), riven_path, only())
        == []
    )


def test_version_outside_riven_library_is_kept():
    local_old = "D:\\Plex\\TV Shows\\Abbott Elementary\\s01e12.mkv"
    assert (
        replaced_versions(
            video(present(), gone(local_old)),
            wanted(NEW),
            riven_path,
            only(NEW_LOCAL),
        )
        == []
    )


def test_version_plex_still_finds_is_kept():
    assert (
        replaced_versions(
            video(present(), gone(exists="1")),
            wanted(NEW),
            riven_path,
            only(NEW_LOCAL),
        )
        == []
    )


def test_last_version_is_never_removed():
    assert (
        replaced_versions(video(gone()), wanted(OLD), riven_path, only(NEW_LOCAL))
        == []
    )


class Section:
    key, type, title = 4, "show", "TV Shows"


class FakeServer:
    def __init__(self, trash_size: int = 1):
        self.deleted = list[str]()
        self.trash_size = trash_size
        self._session = SimpleNamespace(delete=object())

    def query(self, key: str, method=None):
        if method is self._session.delete:
            self.deleted.append(key)
            return None

        if key == "/library/sections":
            return [ET.Element("Directory", key="4", refreshing="0")]

        if "trash=1" in key:
            return [
                ET.Element("Video", ratingKey=str(1610 + i))
                for i in range(self.trash_size)
            ]

        if key.startswith("/library/metadata/1610?"):
            return [video(present(), gone())]

        return [video(present(NEW + "x"))]


def updater(server: FakeServer, enabled: bool = True) -> PlexUpdater:
    updater = object.__new__(PlexUpdater)
    updater.settings = SimpleNamespace(
        path_mappings=MAPPINGS, remove_replaced_versions=enabled
    )
    updater.library_path = LIBRARY
    updater.api = SimpleNamespace(plex_server=server)
    updater.sections = {
        Section(): ["\\\\wsl-media\\riven\\shows"]
    }
    updater.initialized = True
    updater._cleanup_lock = threading.Lock()
    updater._cleanup_paths = set()
    updater._cleanup_timer = None
    return updater


def test_remove_replaced_versions_deletes_the_old_version(monkeypatch):
    monkeypatch.setattr(plex.os.path, "exists", only(NEW_LOCAL))
    server = FakeServer()

    removed = updater(server).remove_replaced_versions([NEW_LOCAL])

    assert server.deleted == ["/library/metadata/1610/media/1937"]
    assert removed == [f"Abbott Elementary S01E12: {OLD}"]


def test_dry_run_deletes_nothing(monkeypatch):
    monkeypatch.setattr(plex.os.path, "exists", only(NEW_LOCAL))
    server = FakeServer()

    assert updater(server).remove_replaced_versions([NEW_LOCAL], dry_run=True)
    assert server.deleted == []


def test_full_trash_means_share_down(monkeypatch):
    monkeypatch.setattr(plex.os.path, "exists", only(NEW_LOCAL))
    server = FakeServer(trash_size=plex.MAX_TRASH + 1)

    assert updater(server).remove_replaced_versions([NEW_LOCAL]) == []
    assert server.deleted == []


def test_schedule_is_off_by_default_and_debounced(monkeypatch):
    started = []

    class Timer:
        def __init__(self, delay, fn):
            started.append(delay)
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(plex.threading, "Timer", Timer)

    off = updater(FakeServer(), enabled=False)
    off.schedule_version_cleanup([NEW_LOCAL])
    assert started == [] and off._cleanup_paths == set()

    on = updater(FakeServer())
    on.schedule_version_cleanup([NEW_LOCAL])
    on.schedule_version_cleanup([OLD_LOCAL])
    assert started == [plex.CLEANUP_DELAY]
    assert on._cleanup_paths == {NEW_LOCAL, OLD_LOCAL}
