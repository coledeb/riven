"""Presentation tweaks for the settings JSON schema served to the frontend.

The frontend builds its settings form from the schema alone, so these only add
titles and choice lists. They don't change what the backend accepts.
"""

from typing import Any

from PTT.parse import LANGUAGES_TRANSLATION_TABLE

# Words that plain title-casing gets wrong.
_WORDS = {
    "3d": "3D",
    "aac": "AAC",
    "aiostreams": "AIOStreams",
    "av1": "AV1",
    "avc": "AVC",
    "bdrip": "BDRip",
    "bit10": "10-bit",
    "brrip": "BRRip",
    "debridio": "Debridio",
    "dts": "DTS",
    "dvd": "DVD",
    "dvdrip": "DVDRip",
    "flac": "FLAC",
    "hdr": "HDR",
    "hdr10plus": "HDR10+",
    "hdrip": "HDRip",
    "hdtv": "HDTV",
    "hevc": "HEVC",
    "mdblist": "MDBList",
    "mp3": "MP3",
    "mpeg": "MPEG",
    "pdtv": "PDTV",
    "ppvrip": "PPVRip",
    "r5": "R5",
    "rtn": "RTN",
    "satrip": "SATRip",
    "sdr": "SDR",
    "torbox": "TorBox",
    "truehd": "TrueHD",
    "tvrip": "TVRip",
    "uhdrip": "UHDRip",
    "vhs": "VHS",
    "vhsrip": "VHSRip",
    "webdl": "WEB-DL",
    "webdlrip": "WEB-DLRip",
    "webmux": "WEBMux",
    "webrip": "WEBRip",
    "xvid": "XviD",
}

# Whole property names that need a different label.
_PROPERTY_TITLES = {
    "account_import": "Account Import",
    "custom_ranks": "Custom Ranks",
    "dolby_digital_plus": "Dolby Digital Plus",
    "dts_lossless": "DTS Lossless",
    "dts_lossy": "DTS Lossy",
    "oauth": "OAuth",
    "overseerr": "Seerr / Overseerr",
    "plex_watchlist": "Plex Watchlist",
    "ranking": "Ranking",
    "scraping": "Scraping",
    "web": "WEB",
}

_LANGUAGE_FIELDS = ("required", "allowed", "exclude", "preferred")


def _title(name: str) -> str:
    if name in _PROPERTY_TITLES:
        return _PROPERTY_TITLES[name]
    return " ".join(_WORDS.get(w, w.capitalize()) for w in name.split("_"))


def _is_ref(prop: dict[str, Any]) -> bool:
    if "$ref" in prop:
        return True
    all_of = prop.get("allOf")
    return isinstance(all_of, list) and len(all_of) == 1 and "$ref" in all_of[0]


def _title_sections(schema: dict[str, Any]) -> None:
    """Name nested sections after their field, not their model class."""
    for name, prop in schema.get("properties", {}).items():
        if isinstance(prop, dict) and _is_ref(prop) and "title" not in prop:
            prop["title"] = _title(name)


def _language_choices() -> dict[str, Any]:
    return {
        "type": "array",
        "uniqueItems": True,
        "items": {
            "type": "string",
            "oneOf": [
                {"const": code, "title": f"{name} ({code})"}
                for code, name in sorted(
                    LANGUAGES_TRANSLATION_TABLE.items(), key=lambda kv: kv[1]
                )
            ],
        },
    }


def polish_settings_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make the generated settings schema read well in the frontend form."""
    defs = schema.get("$defs", {})
    _title_sections(schema)
    for definition in defs.values():
        _title_sections(definition)

    languages = defs.get("LanguagesConfig", {}).get("properties", {})
    for field in _LANGUAGE_FIELDS:
        if field in languages:
            languages[field] = {
                **{k: v for k, v in languages[field].items() if k not in ("items", "type")},
                **_language_choices(),
                "title": _title(field),
            }
    return schema
