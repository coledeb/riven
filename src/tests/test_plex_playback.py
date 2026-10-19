"""Regression: Plex Web audio transcodes omit session Part.file."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from program.services.updaters.plex import PlexUpdater, playing_sessions

PATH = "/mnt/debrid/riven/shows/Lanterns/s01e08.mkv"
UNC = r"\\wsl-media\riven\shows\Lanterns\s01e08.mkv"
MAPPINGS = {"/mnt/debrid/riven": r"\\wsl-media\riven"}


def media_item(*parts, **kwargs):
    return NS(media=[NS(parts=list(parts))], **kwargs)


def test_updater_resolves_transcode_and_returns_stoppable_session():
    session = media_item(NS(id="1014", file=None), ratingKey="798", stop=Mock())
    fetch = Mock(return_value=media_item(NS(id=1014, file=UNC.upper())))
    updater = PlexUpdater.__new__(PlexUpdater)
    updater.api = NS(plex_server=NS(sessions=lambda: [session], fetchItem=fetch))
    updater.settings = NS(path_mappings=MAPPINGS)
    found = updater.sessions_playing([PATH])
    assert found == [session]
    found[0].stop(reason="replace")
    session.stop.assert_called_once_with(reason="replace")
    fetch.assert_called_once_with("798")


def test_direct_play_does_not_fetch_metadata():
    session = media_item(NS(file=UNC))
    fetch = Mock()
    assert playing_sessions([session], [PATH], MAPPINGS, fetch_item=fetch) == [session]
    fetch.assert_not_called()


def test_alternate_version_is_not_mistaken_for_playing_source():
    session = media_item(NS(id=2, file=None), ratingKey="798")
    fetch = Mock(
        return_value=media_item(NS(id=1, file=UNC), NS(id=2, file="D:\\other.mkv"))
    )
    assert playing_sessions([session], [PATH], MAPPINGS, fetch_item=fetch) == []


@pytest.mark.parametrize(
    "part_id,library_parts,expected",
    [
        (None, [NS(id=1, file=UNC)], True),
        (None, [NS(id=1, file=UNC), NS(id=2, file="D:\\other.mkv")], False),
        (99, [NS(id=1, file=UNC)], False),
        (1, [], False),
    ],
)
def test_fallback_only_uses_identified_or_unambiguous_source(
    part_id, library_parts, expected
):
    session = media_item(NS(id=part_id, file=None), ratingKey="798")
    found = playing_sessions(
        [session], [PATH], MAPPINGS, fetch_item=lambda _: media_item(*library_parts)
    )
    assert found == ([session] if expected else [])


def test_failed_lookup_does_not_hide_other_playback():
    missing = media_item(NS(id=1, file=None), ratingKey="798")
    direct = media_item(NS(file=UNC))
    fetch = Mock(side_effect=RuntimeError("sensitive request URL"))
    assert playing_sessions(
        [missing, missing, direct], [PATH], MAPPINGS, fetch_item=fetch
    ) == [direct]
    fetch.assert_called_once()


def test_metadata_shared_within_call_but_refetched_next_call():
    session = media_item(NS(id=1, file=None), ratingKey="798")
    fetch = Mock(return_value=media_item(NS(id=1, file=UNC)))
    assert playing_sessions([session, session], [PATH], MAPPINGS, fetch_item=fetch) == [
        session,
        session,
    ]
    fetch.assert_called_once()
    fetch.return_value = media_item(NS(id=1, file="D:\\changed.mkv"))
    assert playing_sessions([session], [PATH], MAPPINGS, fetch_item=fetch) == []
    assert fetch.call_count == 2
