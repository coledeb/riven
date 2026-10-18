"""Anime audio preference: Dual > Japanese > dubbed, from real Demon Slayer release names."""

import pytest
from RTN import RTN, DefaultRanking, parse
from RTN.models import SettingsModel

from program.services.scrapers.anime_audio import (
    DUAL_BONUS,
    JAPANESE_BONUS,
    anime_audio_bonus,
    with_anime_audio_bonus,
)

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


def bonus(title):
    return anime_audio_bonus(title, parse(title))


@pytest.mark.parametrize("title", DUAL)
def test_dual_audio(title):
    assert bonus(title) == DUAL_BONUS


@pytest.mark.parametrize("title", JAPANESE)
def test_japanese_or_untagged(title):
    assert bonus(title) == JAPANESE_BONUS


@pytest.mark.parametrize("title", DUBBED)
def test_dubbed(title):
    assert bonus(title) == 0


def test_bonus_is_added_to_the_rank():
    dual = rtn.rank(DUAL[0], "a" * 40)
    dubbed = rtn.rank(DUBBED[0], "b" * 40)

    assert with_anime_audio_bonus(dual).rank == dual.rank + DUAL_BONUS
    assert with_anime_audio_bonus(dubbed) is dubbed
