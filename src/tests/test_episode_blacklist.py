"""A dead file in a season pack must not be re-attached to its episode."""

from types import SimpleNamespace

from program.media.item import Episode, Season, Show
from program.media.stream import Stream
from program.services.downloaders import Downloader

PACK = "a" * 40


def make_stream(infohash=PACK):
    torrent = SimpleNamespace(
        raw_title="Show S01 1080p",
        infohash=infohash,
        data=SimpleNamespace(parsed_title="Show", resolution="1080p"),
        rank=100,
        lev_ratio=1.0,
    )
    return Stream(torrent)


def test_blacklisting_a_parent_pack_records_it_on_the_episode():
    episode = Episode({"number": 1})
    episode.parent = Season({"number": 1})
    episode.parent.parent = Show({"title": "Show"})
    pack = make_stream()

    assert pack not in episode.streams
    assert episode.blacklist_stream(pack)
    assert pack in episode.blacklisted_streams
    assert not episode.blacklist_stream(pack)  # already there


def test_pack_match_skips_episode_that_blacklisted_it(monkeypatch):
    show = Show({"title": "Show"})
    season = Season({"number": 1})
    good, dead = Episode({"number": 1}), Episode({"number": 2})
    season.episodes = [good, dead]
    show.seasons = [season]
    for ep in (good, dead):
        ep.parent = season
    season.parent = show
    dead.blacklist_stream(make_stream())

    updated = []
    downloader = object.__new__(Downloader)
    monkeypatch.setattr(
        downloader,
        "_update_attributes",
        lambda ep, *_args, **_kwargs: updated.append(ep.number),
    )

    result = SimpleNamespace(infohash=PACK, info=SimpleNamespace(id=1))
    for n in (1, 2):
        downloader.match_file_to_item(
            item=season,
            file_data=SimpleNamespace(type="show", seasons=[1], episodes=[n]),
            file=SimpleNamespace(filename=f"Show.S01E0{n}.mkv"),
            download_result=result,
            service=object(),
            show=show,
        )

    assert updated == [1]
