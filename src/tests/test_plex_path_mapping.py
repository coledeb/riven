from program.services.updaters import plex
from program.services.updaters.plex import is_windows_path, map_path

WINDOWS = {"/mnt/debrid/riven": "\\\\wsl-media\\riven"}


def test_no_mapping_leaves_paths_alone():
    assert map_path("/mnt/debrid/riven/shows/X", {}) == "/mnt/debrid/riven/shows/X"


def test_unc_target_gets_backslashes():
    path = "/mnt/debrid/riven/shows/Gilmore Girls (2000)/Season 03"
    assert (
        map_path(path, WINDOWS)
        == "\\\\wsl-media\\riven\\shows\\Gilmore Girls (2000)\\Season 03"
    )


def test_drive_letter_target_and_trailing_separators():
    mapping = {"/mnt/debrid/riven/": "D:\\Media\\"}
    assert map_path("/mnt/debrid/riven/movies/X", mapping) == "D:\\Media\\movies\\X"


def test_posix_target_and_longest_prefix_wins():
    mapping = {
        "/mnt/debrid/riven": "/media/riven",
        "/mnt/debrid/riven/movies": "/films",
    }
    assert map_path("/mnt/debrid/riven/movies/X", mapping) == "/films/X"
    assert map_path("/mnt/debrid/riven/shows/Y", mapping) == "/media/riven/shows/Y"


def test_only_whole_path_components_match():
    assert map_path("/mnt/debrid/rivenx/a", WINDOWS) == "/mnt/debrid/rivenx/a"


def test_windows_detection():
    assert is_windows_path("\\\\server\\share")
    assert is_windows_path("D:\\media")
    assert not is_windows_path("/mnt/debrid/riven")


class FakeSection:
    def __init__(self, name):
        self.name = name


def test_refresh_matches_windows_sections_case_insensitively():
    updater = plex.PlexUpdater.__new__(plex.PlexUpdater)
    shows = FakeSection("TV")
    updater.sections = {shows: ["\\\\WSL-MEDIA\\riven\\shows"]}
    updater.settings = type("S", (), {"path_mappings": WINDOWS})()
    calls = []
    updater.api = type(
        "A", (), {"update_section": lambda _self, s, p: calls.append((s, p)) or True}
    )()

    assert updater.refresh_path("/mnt/debrid/riven/shows/X (2000)")
    assert calls == [(shows, "\\\\wsl-media\\riven\\shows\\X (2000)")]


def test_refresh_without_mapping_is_unchanged():
    updater = plex.PlexUpdater.__new__(plex.PlexUpdater)
    shows = FakeSection("TV")
    updater.sections = {shows: ["/mnt/debrid/riven/shows"]}
    updater.settings = type("S", (), {"path_mappings": {}})()
    calls = []
    updater.api = type(
        "A", (), {"update_section": lambda _self, s, p: calls.append((s, p)) or True}
    )()

    assert updater.refresh_path("/mnt/debrid/riven/shows/X")
    assert calls == [(shows, "/mnt/debrid/riven/shows/X")]
    assert not updater.refresh_path("/elsewhere/X")
