"""Send notifications, as Seerr-style Discord embeds where possible.

Discord webhook URLs (``https://discord.com/api/webhooks/...`` or
``discord://id/token``) get one embed laid out like Seerr's: the event as the
author line, the title linked to the item's page, a description, inline
fields, the poster as thumbnail and a colour for the kind of event. Every other
URL goes through Apprise as plain title + text.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import requests
from apprise import Apprise
from loguru import logger

if TYPE_CHECKING:
    from program.media.item import MediaItem

# Seerr's embed colours
ORANGE = 15105570  # pending / attention
PURPLE = 10181046  # in progress
GREEN = 3066993  # available
RED = 15158332  # failed

BOT_NAME = "Riven"
BOT_AVATAR = "https://raw.githubusercontent.com/coledeb/riven/a4448e7b15c8b60f62810bc4a6cbbd5cb9331daf/assets/riven-discord-avatar.png"

DISCORD_HTTPS = re.compile(
    r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/(\d+)/([\w-]+)"
)
DISCORD_SCHEME = re.compile(r"^discord://(?:[^@/]*@)?(\d+)/([\w-]+)")

DESCRIPTION_LIMIT = 300  # Seerr trims to the same length
FIELD_LIMIT = 1024


@dataclass
class Notice:
    event: str
    title: str
    description: str = ""
    color: int = PURPLE
    fields: list[tuple[str, str]] = field(default_factory=list)
    url: str | None = None
    image: str | None = None
    # When it happened (default: now)
    timestamp: datetime | None = None

    def with_item(self, item: "MediaItem", link_base: str = "") -> "Notice":
        """Fill the title link and poster from an item (shows use the show's poster)."""

        top = item
        while getattr(top, "parent", None) is not None:
            top = top.parent

        self.image = self.image or getattr(top, "poster_path", None)
        kind = "movie" if getattr(top, "type", None) == "movie" else "tv"
        tmdb_id = getattr(top, "tmdb_id", None)
        tvdb_id = getattr(top, "tvdb_id", None)

        if self.url is not None:
            return self

        if not tmdb_id and kind == "tv" and tvdb_id:
            tmdb_id = _tmdb_for_tvdb(str(tvdb_id))

        if tmdb_id:
            base = link_base.rstrip("/") or "https://www.themoviedb.org"
            self.url = f"{base}/{kind}/{tmdb_id}"
        elif tvdb_id:
            self.url = f"https://thetvdb.com/dereferrer/series/{tvdb_id}"

        return self


_tmdb_ids = dict[str, str | None]()


def _tmdb_for_tvdb(tvdb_id: str) -> str | None:
    """Shows only store a TVDB id; Seerr links need the TMDB one."""

    if tvdb_id not in _tmdb_ids:
        try:
            from kink import di

            from program.apis.tmdb_api import TMDBApi

            _tmdb_ids[tvdb_id] = di[TMDBApi].find_id("tv", "tvdb_id", tvdb_id)
        except Exception as e:
            logger.debug(f"Notification: no TMDB id for TVDB {tvdb_id}: {e}")
            return None

    return _tmdb_ids[tvdb_id]


def item_title(item: "MediaItem") -> str:
    """'Title (Year)' for movies and shows, 'Show (Year) S01E02' below them."""

    top = item
    while getattr(top, "parent", None) is not None:
        top = top.parent

    name = getattr(top, "title", None) or item.log_string
    if aired_at := getattr(top, "aired_at", None):
        name = f"{name} ({aired_at.year})"

    if top is item:
        return name

    suffix = item.log_string.removeprefix(getattr(top, "log_string", ""))
    return f"{name}{suffix}"


def notice_for_item(notice: Notice, item_id: int | str | None) -> Notice:
    """Load the item by id and fill the notice from it; no-op if it's gone."""

    if item_id is None:
        return notice

    from program.db.db import db_session
    from program.media.item import MediaItem
    from program.settings import settings_manager

    try:
        with db_session() as session:
            item = session.get(MediaItem, int(item_id))
            if item is not None:
                notice.with_item(item, settings_manager.settings.notifications.link_url)
    except Exception as e:
        logger.debug(f"Notification: couldn't load item {item_id}: {e}")

    return notice


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _embed(notice: Notice) -> dict:
    embed: dict = {
        "author": {"name": notice.event},
        "title": _clip(notice.title, 256),
        "color": notice.color,
        "timestamp": (notice.timestamp or datetime.now(timezone.utc)).isoformat(),
    }

    if notice.url:
        embed["url"] = notice.url
    if notice.description:
        embed["description"] = _clip(notice.description, DESCRIPTION_LIMIT)
    if notice.image:
        embed["thumbnail"] = {"url": notice.image}

    fields = [
        {"name": name, "value": _clip(value, FIELD_LIMIT), "inline": len(value) <= 40}
        for name, value in notice.fields
        if value
    ]
    if fields:
        embed["fields"] = fields

    return embed


def _discord_webhook(url: str) -> str | None:
    if match := DISCORD_HTTPS.match(url) or DISCORD_SCHEME.match(url):
        return f"https://discord.com/api/webhooks/{match[1]}/{match[2]}"

    return None


def send(urls: list[str], notice: Notice) -> None:
    """Send the notice to every URL; failures are logged, never raised."""

    others = Apprise()

    for url in urls:
        webhook = _discord_webhook(url)

        if webhook is None:
            others.add(url)
            continue

        try:
            response = requests.post(
                webhook,
                json={
                    "username": BOT_NAME,
                    "avatar_url": BOT_AVATAR,
                    "embeds": [_embed(notice)],
                },
                timeout=10,
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning(f"Discord notification failed: {e}")

    if len(others) == 0:
        return

    lines = [notice.description] if notice.description else []
    lines += [f"{name}: {value}" for name, value in notice.fields if value]

    try:
        others.notify(title=f"{notice.event}: {notice.title}", body="\n".join(lines))
    except Exception as e:
        logger.warning(f"Notification failed: {e}")
