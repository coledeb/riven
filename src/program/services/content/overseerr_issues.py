"""
Report movies and episodes Riven can't find a file for as Overseerr issues.

A released movie or episode (past its release date plus a grace period) that
stays without a file for a while gets an Overseerr issue, so requesters can see
it is stuck. Episodes are grouped per season: one issue per season, with a
comment when more episodes go missing. The issue is resolved, with a comment,
once nothing in it is missing any more.

Files that stop playing are covered too: Riven resets items whose link is dead,
which makes them missing until a new source is found.

State (when each item first went missing, which issues are open) is kept in
`overseerr_issues.json` in the data folder, so restarts don't reset the clock.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kink import di
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import aliased

from program.apis.overseerr_api import OverseerrAPI
from program.db.db import db_session
from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.state import States
from program.settings import settings_manager
from program.utils import data_dir_path

# Items in these states are not missing (done, not out yet, or paused on purpose).
NOT_MISSING = (
    States.Completed,
    States.Symlinked,
    States.Downloaded,
    States.Unreleased,
    States.Paused,
)


@dataclass
class Gap:
    """A movie, or the missing episodes of one season."""

    item_id: int
    label: str
    kind: str  # "movie" or "tv"
    tmdb_id: str | None
    tvdb_id: str | None
    season: int = 0
    episodes: list[int] = field(default_factory=list)
    states: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{'movie' if self.kind == 'movie' else 'season'}:{self.item_id}"


def _episode_list(season: int, episodes: list[int]) -> str:
    return ", ".join(f"S{season:02d}E{e:02d}" for e in sorted(episodes))


class OverseerrIssues:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state_file = data_dir_path / "overseerr_issues.json"

    @property
    def settings(self):
        return settings_manager.settings.content.overseerr.issues

    def run(self) -> None:
        if not self._lock.acquire(blocking=False):
            return

        try:
            self._run(datetime.now())
        except Exception as e:
            logger.warning(f"Overseerr issues: check failed: {e}")
        finally:
            self._lock.release()

    # --- state ---

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self._state_file.read_text())
        except (OSError, ValueError):
            state = {}

        state.setdefault("missing_since", {})
        state.setdefault("open", {})
        return state

    def _save(self, state: dict[str, Any]) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(self._state_file)

    # --- library ---

    def _gaps(self, now: datetime) -> dict[str, Gap]:
        cutoff = now - timedelta(hours=self.settings.release_grace_hours)
        gaps = dict[str, Gap]()
        season = aliased(Season)
        show = aliased(Show)

        with db_session() as session:
            movies = session.execute(
                select(
                    Movie.id,
                    Movie.title,
                    Movie.aired_at,
                    Movie.tmdb_id,
                    Movie.tvdb_id,
                    Movie.last_state,
                ).where(
                    Movie.last_state.not_in(NOT_MISSING),
                    Movie.aired_at <= cutoff,
                )
            ).all()

            episodes = session.execute(
                select(
                    Episode.number,
                    Episode.last_state,
                    season.id.label("season_id"),
                    season.number.label("season"),
                    show.title,
                    show.tmdb_id,
                    show.tvdb_id,
                )
                .join(season, Episode.parent_id == season.id)
                .join(show, season.parent_id == show.id)
                .where(
                    Episode.last_state.not_in(NOT_MISSING),
                    Episode.aired_at <= cutoff,
                )
            ).all()

        for m in movies:
            year = f" ({m.aired_at.year})" if m.aired_at else ""
            gap = Gap(m.id, f"{m.title}{year}", "movie", m.tmdb_id, m.tvdb_id)
            gap.states.add(m.last_state.value)
            gaps[gap.key] = gap

        for e in episodes:
            gap = Gap(
                e.season_id,
                f"{e.title} season {e.season}",
                "tv",
                e.tmdb_id,
                e.tvdb_id,
                season=e.season,
            )
            gap = gaps.setdefault(gap.key, gap)
            gap.episodes.append(e.number)
            gap.states.add(e.last_state.value)

        return gaps

    def _closing_note(self, key: str) -> str:
        item_id = int(key.split(":", 1)[1])

        with db_session() as session:
            row = session.execute(
                select(MediaItem.last_state).where(MediaItem.id == item_id)
            ).first()

        if row is None:
            return "Removed from Riven, closing."

        if row.last_state == States.Paused:
            return "Paused in Riven, closing."

        return "Fixed: available again."

    # --- one check ---

    def _run(self, now: datetime) -> None:
        api = di[OverseerrAPI]
        user = self.settings.user_id
        gaps = self._gaps(now)
        state = self._load()
        since: dict[str, float] = state["missing_since"]
        opened: dict[str, dict[str, Any]] = state["open"]
        stamp = now.timestamp()

        for key in list(since):
            if key not in gaps:
                del since[key]

        for key in gaps:
            since.setdefault(key, stamp)

        try:
            for key, issue in list(opened.items()):
                if key in gaps:
                    continue

                note = self._closing_note(key)

                if api.comment_issue(issue["id"], note, user):
                    api.resolve_issue(issue["id"], user)

                logger.info(
                    f"Overseerr issues: resolved #{issue['id']} for {issue['label']} ({note})"
                )
                del opened[key]

            media: dict[tuple[str, str], int] | None = None

            for key, gap in gaps.items():
                if key in opened:
                    issue = opened[key]
                    new = sorted(set(gap.episodes) - set(issue.get("episodes", [])))

                    if new and not api.comment_issue(
                        issue["id"],
                        f"Also missing now: {_episode_list(gap.season, new)}.",
                        user,
                    ):
                        # Deleted in Overseerr: open a fresh one next time
                        del opened[key]
                        since[key] = stamp
                        continue

                    issue["episodes"] = sorted(gap.episodes)
                    continue

                if stamp - since[key] < self.settings.after_minutes * 60:
                    continue

                if media is None:
                    media = api.media_ids()

                media_id = (
                    media.get((gap.kind, str(gap.tmdb_id)))
                    if gap.tmdb_id
                    else None
                ) or (media.get(("tvdb", str(gap.tvdb_id))) if gap.kind == "tv" else None)

                if not media_id:
                    logger.debug(
                        f"Overseerr issues: {gap.label} is not in Overseerr, skipping"
                    )
                    continue

                missing = (
                    _episode_list(gap.season, gap.episodes)
                    if gap.kind == "tv"
                    else "This movie"
                )
                started = datetime.fromtimestamp(since[key]).strftime("%Y-%m-%d %H:%M")
                message = (
                    f"{missing} {'has' if gap.kind == 'movie' or len(gap.episodes) == 1 else 'have'} "
                    f"no working file in Riven yet (missing since {started}; "
                    f"state: {', '.join(sorted(gap.states))}). Riven keeps looking; "
                    "this issue closes itself once it's available."
                )
                issue_id = api.create_issue(
                    media_id,
                    message,
                    season=gap.season,
                    episode=gap.episodes[0] if len(gap.episodes) == 1 else 0,
                    user_id=user,
                )
                opened[key] = {
                    "id": issue_id,
                    "label": gap.label,
                    "episodes": sorted(gap.episodes),
                }
                logger.info(f"Overseerr issues: opened #{issue_id} for {gap.label}")
        finally:
            self._save(state)


overseerr_issues_service = OverseerrIssues()


def overseerr_issues() -> None:
    """Scheduled entry point."""

    overseerr_issues_service.run()
