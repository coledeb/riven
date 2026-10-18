"""Anime audio preference: Dual (Japanese + English) > Japanese > dubbed.

Anime release names rarely state their audio. Untagged releases (SubsPlease,
Erai-raws, most WEB-DLs) carry the Japanese track, and "ESub" / "English
Subbed" mean subtitles, not audio. Only explicit dual-audio wording counts as
dual, and only RTN's `dubbed` flag (without dual wording) counts as a dub.
"""

import regex
from RTN import ParsedData, Torrent

DUAL_BONUS = 10000
JAPANESE_BONUS = 5000

_DUAL = regex.compile(
    r"dual[\s._-]*audio|\bdual\b"
    r"|\b(?:jpn?|jap(?:anese)?)[\s._&+/-]+eng(?:lish)?\b(?![\s._-]*sub)"
    r"|\beng(?:lish)?[\s._&+/-]+(?:jpn?|jap(?:anese)?)\b",
    regex.IGNORECASE,
)


def anime_audio_bonus(raw_title: str, data: ParsedData) -> int:
    """Rank bonus for an anime release, by how likely it is to have each audio track."""
    if _DUAL.search(raw_title):
        return DUAL_BONUS
    if data.dubbed and "multi" not in raw_title.lower():
        return 0
    return JAPANESE_BONUS


def with_anime_audio_bonus(torrent: Torrent) -> Torrent:
    """Copy of a ranked (frozen) torrent with the anime audio bonus added to its rank."""
    bonus = anime_audio_bonus(torrent.raw_title, torrent.data)
    return (
        torrent.model_copy(update={"rank": torrent.rank + bonus}) if bonus else torrent
    )
