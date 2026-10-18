"""Anime audio preference, in the order set by `scraping.anime_audio_order`.

Anime release names rarely state their audio. Untagged releases (SubsPlease,
Erai-raws, most WEB-DLs) carry the Japanese track, and "ESub" / "English
Subbed" mean subtitles, not audio. Only explicit dual-audio wording counts as
dual, and only RTN's `dubbed` flag (without dual wording) counts as a dub.
"""

from collections.abc import Sequence
from typing import Literal

import regex
from RTN import ParsedData, Torrent

AudioType = Literal["dual", "japanese", "dubbed"]

# Rank added per place in the order: with three types, the first gets 3 steps,
# the last 1 step and unlisted types none. Large enough to outweigh quality
# differences within a resolution.
STEP = 5000

_DUAL = regex.compile(
    r"dual[\s._-]*audio|\bdual\b"
    r"|\b(?:jpn?|jap(?:anese)?)[\s._&+/-]+eng(?:lish)?\b(?![\s._-]*sub)"
    r"|\beng(?:lish)?[\s._&+/-]+(?:jpn?|jap(?:anese)?)\b",
    regex.IGNORECASE,
)


def audio_type(raw_title: str, data: ParsedData) -> AudioType:
    """Best guess at an anime release's audio from its name."""
    if _DUAL.search(raw_title):
        return "dual"
    if data.dubbed and "multi" not in raw_title.lower():
        return "dubbed"
    return "japanese"


def anime_audio_bonus(
    raw_title: str, data: ParsedData, order: Sequence[AudioType]
) -> int:
    """Rank bonus for an anime release: higher the earlier its audio type is in `order`."""
    kind = audio_type(raw_title, data)
    if kind not in order:
        return 0
    return (len(order) - order.index(kind)) * STEP


def with_anime_audio_bonus(torrent: Torrent, order: Sequence[AudioType]) -> Torrent:
    """Copy of a ranked (frozen) torrent with the anime audio bonus added to its rank."""
    bonus = anime_audio_bonus(torrent.raw_title, torrent.data, order)
    return (
        torrent.model_copy(update={"rank": torrent.rank + bonus}) if bonus else torrent
    )
