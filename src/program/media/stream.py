from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import sqlalchemy
from RTN import Torrent
from sqlalchemy import Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from program.db.base_model import Base


if TYPE_CHECKING:
    from program.media.item import MediaItem


class StreamRelation(Base):
    __tablename__ = "StreamRelation"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("MediaItem.id", ondelete="CASCADE")
    )
    child_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("Stream.id", ondelete="CASCADE")
    )

    __table_args__ = (
        Index("ix_streamrelation_parent_id", "parent_id"),
        Index("ix_streamrelation_child_id", "child_id"),
    )


class BlacklistReason(StrEnum):
    """Why a stream was blacklisted for an item; decides whether it expires."""

    CDN_HOST_DOWN = "cdn_host_down"  # its CDN server was pulled (0.0.0.0)
    SLOW_SOURCE = "slow_source"  # too slow to play (stream.slow_replace)
    NOT_CACHED = "not_cached"  # not cached on the debrid service when tried
    TORRENT_REMOVED = "torrent_removed"  # torrent or file gone from the account
    DOWNLOAD_ERROR = "download_error"  # any other download failure
    DOWNLOAD_STALLED = "download_stalled"  # uncached download made no progress
    WRONG_FILES = "wrong_files"  # has no file for the item (permanent)
    MANUAL = "manual"  # blacklisted by the user (permanent)
    UNKNOWN = "unknown"  # permanent


_EXPIRY_SETTING = {
    BlacklistReason.CDN_HOST_DOWN: "cdn_host_down_hours",
    BlacklistReason.SLOW_SOURCE: "slow_source_hours",
    BlacklistReason.NOT_CACHED: "not_cached_hours",
    BlacklistReason.TORRENT_REMOVED: "torrent_removed_hours",
    BlacklistReason.DOWNLOAD_ERROR: "download_error_hours",
    BlacklistReason.DOWNLOAD_STALLED: "download_error_hours",
}


def blacklist_expiry(
    reason: BlacklistReason, strikes: int, now: datetime
) -> datetime | None:
    """When a blacklisting expires (None = never): the reason's hours, doubled per extra strike."""

    from program.settings import settings_manager

    config = settings_manager.settings.scraping.blacklist
    setting = _EXPIRY_SETTING.get(reason)

    if not config.expiry_enabled or setting is None:
        return None

    hours = getattr(config, setting)

    if hours <= 0 or (config.max_strikes and strikes > config.max_strikes):
        return None

    return now + timedelta(hours=hours * 2 ** (strikes - 1))


class StreamBlacklistRelation(Base):
    __tablename__ = "StreamBlacklistRelation"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("MediaItem.id", ondelete="CASCADE")
    )
    stream_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("Stream.id", ondelete="CASCADE")
    )
    reason: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    blacklisted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # None = permanent
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Set once expired and back in the item's streams; the row stays to count strikes
    released_at: Mapped[datetime | None] = mapped_column(nullable=True)
    strikes: Mapped[int] = mapped_column(default=1, server_default="1")
    stream: Mapped["Stream"] = relationship(lazy="selectin")

    __table_args__ = (
        Index("ix_streamblacklistrelation_media_item_id", "media_item_id"),
        Index("ix_streamblacklistrelation_stream_id", "stream_id"),
        Index("ix_streamblacklistrelation_expires_at", "expires_at"),
    )


class Stream(Base):
    __tablename__ = "Stream"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    infohash: Mapped[str]
    raw_title: Mapped[str]
    parsed_title: Mapped[str]
    rank: Mapped[int]
    lev_ratio: Mapped[float]
    resolution: Mapped[str | None]
    # Reported by the scraper (Torrentio) at the last scrape; None = unknown
    seeders: Mapped[int | None] = mapped_column(nullable=True, default=None)
    is_cached: bool = False
    parents: Mapped[list["MediaItem"]] = relationship(
        secondary="StreamRelation", back_populates="streams", lazy="selectin"
    )
    blacklisted_parents: Mapped[list["MediaItem"]] = relationship(
        secondary="StreamBlacklistRelation",
        lazy="selectin",
        viewonly=True,
    )

    __table_args__ = (
        Index("ix_stream_infohash", "infohash"),
        Index("ix_stream_raw_title", "raw_title"),
        Index("ix_stream_parsed_title", "parsed_title"),
        Index("ix_stream_rank", "rank"),
        Index("ix_stream_resolution", "resolution"),
    )

    def __init__(self, torrent: Torrent, seeders: int | None = None):
        self.seeders = seeders
        self.raw_title = torrent.raw_title
        self.infohash = torrent.infohash
        self.parsed_title = torrent.data.parsed_title
        self.parsed_data = torrent.data
        self.rank = torrent.rank
        self.lev_ratio = torrent.lev_ratio
        self.resolution = (
            torrent.data.resolution.lower() if torrent.data.resolution else "unknown"
        )
        self.is_cached = False
        # is_cached is handled by its default value in the mapped_column definition

    def __hash__(self):
        return hash(self.infohash)

    def __eq__(self, other: Any):
        return isinstance(other, Stream) and self.infohash == other.infohash

    def to_dict(self):
        """Convert stream to dictionary for API serialization"""

        return {
            "id": self.id,
            "infohash": self.infohash,
            "raw_title": self.raw_title,
            "parsed_title": self.parsed_title,
            "rank": self.rank,
            "lev_ratio": self.lev_ratio,
            "resolution": self.resolution,
            "seeders": self.seeders,
            "is_cached": self.is_cached,
        }
