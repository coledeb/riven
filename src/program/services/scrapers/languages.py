"""Language hints from Stremio addon stream titles.

Torrentio and Debridio list a torrent's languages as country flags on a
separate title line ("🇬🇧 / 🇷🇺", "🌐 Multi Audio|🇫🇷"). Torrent names often
lack them (e.g. Cyrillic-only names of Russian dubs), so scrapers append them
as words RTN parses, letting Riven's language filters apply.
"""

import re

# Country flags -> language words RTN recognises (uppercase: RTN does not
# match "French").
FLAG_LANGUAGES = {
    "GB": "ENGLISH",
    "US": "ENGLISH",
    "FR": "FRENCH",
    "ES": "SPANISH",
    "MX": "LATINO",
    "IT": "ITALIAN",
    "DE": "GERMAN",
    "RU": "RUSSIAN",
    "UA": "UKRAINIAN",
    "PL": "POLISH",
    "PT": "PORTUGUESE",
    "BR": "PORTUGUESE",
    "NL": "DUTCH",
    "SE": "SWEDISH",
    "NO": "NORWEGIAN",
    "DK": "DANISH",
    "FI": "FINNISH",
    "CZ": "CZECH",
    "SK": "SLOVAK",
    "HU": "HUNGARIAN",
    "RO": "ROMANIAN",
    "BG": "BULGARIAN",
    "GR": "GREEK",
    "TR": "TURKISH",
    "IL": "HEBREW",
    "SA": "ARABIC",
    "IR": "PERSIAN",
    "IN": "HINDI",
    "CN": "CHINESE",
    "TW": "CHINESE",
    "JP": "JAPANESE",
    "KR": "KOREAN",
    "TH": "THAI",
    "VN": "VIETNAMESE",
    "ID": "INDONESIAN",
    "RS": "SERBIAN",
    "HR": "CROATIAN",
    "SI": "SLOVENIAN",
    "LT": "LITHUANIAN",
    "LV": "LATVIAN",
    "EE": "ESTONIAN",
}

SEPARATORS = re.compile(r"[|/]")


def flag_languages(line: str) -> str:
    """Language words for one flag line, e.g. "MULTI ENGLISH RUSSIAN"."""

    words = list[str]()

    for raw in SEPARATORS.split(line.replace("🌐", "")):
        part = raw.strip()
        if part in ("Multi Audio", "Dual Audio"):
            words.append(part.split()[0].upper())
        # A flag is two regional-indicator characters.
        elif len(part) == 2 and all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in part):
            code = "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in part)
            if language := FLAG_LANGUAGES.get(code):
                words.append(language)

    return " ".join(dict.fromkeys(words))


def with_languages(title: str, languages: str) -> str:
    return f"{title} [{languages}]" if languages else title
