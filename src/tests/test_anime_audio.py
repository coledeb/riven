"""Anime audio preference in a configurable order, using real Demon Slayer release names."""

import pytest
from RTN import RTN, DefaultRanking, parse
from RTN.models import SettingsModel

from program.services.scrapers.anime_audio import (
    STEP,
    anime_audio_bonus,
    audio_type,
    with_anime_audio_bonus,
)
from program.settings.models import ScraperModel

rtn = RTN(SettingsModel(), DefaultRanking())

DUAL = [
    "Demon Slayer Kimetsu no Yaiba Season 1-5 Specials (Dual Audio)(1080p)(Plex Naming) [DUAL]",
    "Demon.Slayer.S03.Entertainment.District.Arc.1080p.WEB-DL.H.264.AAC.2.0.Dual-FiBERHD [DUAL]",
    "Demon.Slayer.Kimetsu.no.Yaiba.SEASON.01.S01.COMPLETE.DUAL-AUDIO.JPN-ENG.1080p.10bit.BluRay.2CH.x265.HEVC-PSA",
    "[Group] Kimetsu no Yaiba S01 [1080p] [JAP-ENG]",
]
JAPANESE = [
    "Demon.Slayer.Kimetsu.no.Yaiba.S01.1080p.AMZN.WEB-DL.DDP2.0.H.264-KQRM",
    "Demon.Slayer.Kimetsu.no.Yaiba.S05.1080p.CR.WEB-DL.AAC2.0.H.264-SubsPlease",
    "Demon.Slayer.Kimetsu.no.Yaiba.S01.1080p.CR.WEB-DL.AAC2.0.x264-HorribleSubs[rartv] [JAPANESE]",
    "Demon.Slayer.Kimetsu.no.Yaiba.S01.1080P.x265.10bit.ENG.AAC.SUBS [ENGLISH]",
    "Demon.Slayer.Kimetsu.No.Yaiba.S03.Entertainment.District.Arc.English.Subbed.1080p.WEB-DL.H.264.AAC.2.0-FiBERHD",
    "Demon.Slayer.Kimetsu.no.Yaiba.S05E04.1080p.JC.WEB-DL.JPN.AAC2.0.H.264.ESub-ToonsHub.mkv [ENGLISH]",
    "Demon.Slayer.Kimetsu.no.Yaiba.S01.JPN.ENG.SUBS.1080p.WEB-DL",
    "Kimetsu.no.Yaiba.2019.S01.CUSTOM.MULTi.1080p.BluRay.x265-SceneGuardians [MULTI]",
]
DUBBED = [
    "Demon Slayer Kimetsu No Yaiba S01E23 DUBBED 720p HDTV x264-W4F",
    "[Yameii] Demon Slayer - S05E02 [English Dub] [CR WEB-DL 720p] [7302799B].mkv [ENGLISH]",
    "Demon.Slayer_Kimetsu.no.Yaiba_S01E02_Trainer.Sakonji.Urokodaki_English Dubbed.2ch.720p.mp4 [ENGLISH]",
]


@pytest.mark.parametrize(
    "titles, kind", [(DUAL, "dual"), (JAPANESE, "japanese"), (DUBBED, "dubbed")]
)
def test_audio_type(titles, kind):
    for title in titles:
        assert audio_type(title, parse(title)) == kind, title


def bonus(title, order):
    return anime_audio_bonus(title, parse(title), order)


def test_order_sets_the_ranking():
    order = ["dual", "japanese", "dubbed"]
    assert bonus(DUAL[0], order) > bonus(JAPANESE[0], order) > bonus(DUBBED[0], order)

    order = ["dubbed", "dual", "japanese"]
    assert bonus(DUBBED[0], order) > bonus(DUAL[0], order) > bonus(JAPANESE[0], order)

    order = ["japanese", "dubbed", "dual"]
    assert bonus(JAPANESE[0], order) > bonus(DUBBED[0], order) > bonus(DUAL[0], order)


def test_unlisted_types_rank_lowest():
    order = ["japanese"]
    assert bonus(JAPANESE[0], order) == STEP
    assert bonus(DUAL[0], order) == 0
    assert bonus(DUBBED[0], order) == 0


def test_empty_order_is_off():
    assert bonus(DUAL[0], []) == 0


def test_bonus_is_added_to_the_rank():
    order = ["dual", "japanese"]
    dual = rtn.rank(DUAL[0], "a" * 40)
    dubbed = rtn.rank(DUBBED[0], "b" * 40)

    assert with_anime_audio_bonus(dual, order).rank == dual.rank + 2 * STEP
    assert with_anime_audio_bonus(dubbed, order) is dubbed


def test_setting_validation():
    default = ScraperModel()
    assert default.anime_audio_order == ["dual", "japanese", "dubbed"]
    assert default.anime_audio_order_enabled is False
    # Old settings: empty list meant off, partial lists get the rest appended.
    assert ScraperModel(anime_audio_order=[]).anime_audio_order_enabled is False
    partial = ScraperModel(anime_audio_order=["dubbed", "dual"])
    assert partial.anime_audio_order == ["dubbed", "dual", "japanese"]
    assert partial.anime_audio_order_enabled is True
    assert (
        ScraperModel(
            anime_audio_order=["japanese"], anime_audio_order_enabled=False
        ).anime_audio_order_enabled
        is False
    )
    with pytest.raises(ValueError):
        ScraperModel(anime_audio_order=["dual", "dual"])
    with pytest.raises(ValueError):
        ScraperModel(anime_audio_order=["english"])
