"""
Replace what users report in Overseerr/Seerr.

A user opening an issue on a movie, a season or an episode means that copy is
bad, so Riven replaces it (the same as the Replace button: Plex playback of it
is stopped with a message, the current source is blacklisted and another copy
is found, keeping the file in place). The issue gets a comment when that starts
and a comment plus "resolved" once the new copy is in.

Not replaced:
- issues for a whole show ("All Seasons"): a comment asks which season or episode
- issues Riven opened itself (missing files), or opened by the comment user
- issues opened before this was turned on
- an item replaced within `cooldown_hours`: a comment, and the issue stays open

State is kept in `overseerr_replace.json` in the data folder.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from kink import di
from loguru import logger
from sqlalchemy import select

from program.apis.overseerr_api import OverseerrAPI
from program.db.db import db_session
from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.state import States
from program.media.stream import Stream
from program.settings import settings_manager
from program.utils import data_dir_path
from program.utils.notify import PURPLE, Notice, send

DONE = (States.Completed, States.Symlinked)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _label(item: MediaItem) -> str:
    if isinstance(item, Episode):
        return f"{item.parent.parent.title} S{item.parent.number:02d}E{item.number:02d}"

    if isinstance(item, Season):
        return f"{item.parent.title} season {item.number}"

    year = f" ({item.aired_at.year})" if getattr(item, "aired_at", None) else ""
    return f"{item.title}{year}"


def _leaves(item: MediaItem) -> list[MediaItem]:
    return list(item.episodes) if isinstance(item, Season) else [item]


class OverseerrReplace:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state_file = data_dir_path / "overseerr_replace.json"
        self._own_issues_file = data_dir_path / "overseerr_issues.json"

    @property
    def settings(self):
        return settings_manager.settings.content.overseerr.replace_issues

    def run(self) -> None:
        if not self.settings.enabled or OverseerrAPI not in di:
            return

        if not self._lock.acquire(blocking=False):
            return

        try:
            self._run(datetime.now(timezone.utc))
        except Exception as e:
            logger.warning(f"Overseerr replace: check failed: {e}")
        finally:
            self._lock.release()

    # --- state ---

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self._state_file.read_text())
        except (OSError, ValueError):
            state = {}

        state.setdefault("handled", {})
        state.setdefault("replaced", {})
        return state

    def _save(self, state: dict[str, Any]) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(self._state_file)

    def _own_issue_ids(self) -> set[int]:
        """Issues the missing-file reporter opened."""

        try:
            opened = json.loads(self._own_issues_file.read_text()).get("open", {})
        except (OSError, ValueError):
            return set()

        return {issue["id"] for issue in opened.values()}

    def _skip_users(self) -> set[int]:
        overseerr = settings_manager.settings.content.overseerr
        users = {self.settings.user_id}

        if overseerr.issues.enabled:
            users.add(overseerr.issues.user_id)

        return users - {0}

    # --- library ---

    def _find(self, issue: dict[str, Any]) -> tuple[int | None, str | None]:
        """Riven item id for an issue, or None plus why not."""

        media = issue.get("media") or {}
        tmdb_id = media.get("tmdbId")
        tvdb_id = media.get("tvdbId")
        season = issue.get("problemSeason") or 0
        episode = issue.get("problemEpisode") or 0

        with db_session() as session:
            if media.get("mediaType") == "movie":
                movie = session.execute(
                    select(Movie.id).where(Movie.tmdb_id == str(tmdb_id))
                ).first()
                return (movie.id, None) if movie else (None, "not in Riven")

            show = None

            for column, value in ((Show.tmdb_id, tmdb_id), (Show.tvdb_id, tvdb_id)):
                if value and show is None:
                    show = (
                        session.execute(select(Show).where(column == str(value)))
                        .scalars()
                        .first()
                    )

            if not show:
                return None, "not in Riven"

            if not season:
                return None, "whole show"

            match = next((s for s in show.seasons if s.number == season), None)

            if match and episode:
                match = next((e for e in match.episodes if e.number == episode), None)

            return (match.id, None) if match else (None, "not in Riven")

    def _progress(self, item_id: int) -> tuple[str, str]:
        """("done", release) / ("failed", "") / ("gone", "") / ("waiting", "")."""

        with db_session() as session:
            item = session.get(MediaItem, item_id)

            if item is None:
                return "gone", ""

            leaves = [leaf for leaf in _leaves(item) if leaf.last_state != States.Unreleased]

            if any(leaf.last_state == States.Failed for leaf in leaves):
                return "failed", ""

            if not all(leaf.last_state in DONE for leaf in leaves):
                return "waiting", ""

            hashes = {leaf.active_stream.infohash for leaf in leaves if leaf.active_stream}
            titles = (
                session.execute(
                    select(Stream.raw_title).where(Stream.infohash.in_(hashes))
                )
                .scalars()
                .all()
                if hashes
                else []
            )

            return "done", ", ".join(sorted(set(titles))[:3])

    def _replace(self, item_id: int, issue_id: int) -> tuple[str, MediaItem | None]:
        from routers.secure.items import replace_item

        with db_session() as session:
            item = session.get(MediaItem, item_id)

            if item is None:
                return "", None

            label = _label(item)
            replace_item(session, item, source=f"Overseerr issue #{issue_id}")
            return label, item

    # --- one check ---

    def _run(self, now: datetime) -> None:
        api = di[OverseerrAPI]
        user = self.settings.user_id
        state = self._load()

        if "since" not in state:
            # First run: issues already open aren't ours to act on
            state["since"] = now.isoformat()
            self._save(state)
            logger.info("Overseerr replace: on; acting on issues opened from now on")
            return

        since = _parse_time(state["since"])
        handled: dict[str, dict[str, Any]] = state["handled"]
        replaced: dict[str, float] = state["replaced"]
        issues = api.open_issues()
        open_ids = {str(i["id"]) for i in issues}

        try:
            self._follow_up(api, handled, open_ids, user)

            own = self._own_issue_ids()
            skip_users = self._skip_users()

            for issue in reversed(issues):  # oldest first
                key = str(issue["id"])

                if key in handled or _parse_time(issue["createdAt"]) < since:
                    continue

                if issue["id"] in own or (issue.get("createdBy") or {}).get("id") in skip_users:
                    handled[key] = {"status": "ignored"}
                    continue

                self._handle(api, issue, handled, replaced, now, user)
        finally:
            # Forget issues that are closed and done with
            for key in list(handled):
                if key not in open_ids and handled[key]["status"] != "replacing":
                    del handled[key]

            cutoff = now.timestamp() - max(self.settings.cooldown_hours, 1) * 3600
            for key in [k for k, t in replaced.items() if t < cutoff]:
                del replaced[key]

            self._save(state)

    def _handle(
        self,
        api: OverseerrAPI,
        issue: dict[str, Any],
        handled: dict[str, dict[str, Any]],
        replaced: dict[str, float],
        now: datetime,
        user: int,
    ) -> None:
        key = str(issue["id"])
        item_id, reason = self._find(issue)

        if reason == "whole show":
            api.comment_issue(
                issue["id"],
                "Riven replaces a season or an episode, not a whole show. Open a new "
                "issue with the season (and episode, if it's just one) picked, and "
                "Riven will find another copy.",
                user,
            )
            handled[key] = {"status": "asked"}
            logger.info(f"Overseerr replace: issue #{key} is for a whole show, asked which part")
            return

        if item_id is None:
            handled[key] = {"status": "unknown"}
            logger.info(f"Overseerr replace: issue #{key} is for something not in Riven, skipping")
            return

        # Another open issue already started a replace of this item: just follow it
        for other_key, other in handled.items():
            if other.get("status") == "replacing" and other.get("item_id") == item_id:
                api.comment_issue(
                    issue["id"],
                    f"Already finding another copy (issue #{other_key}). This "
                    "issue resolves itself once the new copy is in.",
                    user,
                )
                handled[key] = {**other, "follows": other_key}
                return

        last = replaced.get(str(item_id))
        hours = self.settings.cooldown_hours

        if last and now.timestamp() - last < hours * 3600:
            at = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M")
            api.comment_issue(
                issue["id"],
                f"Riven already replaced this at {at} and won't replace it again "
                f"within {hours} hours. Leaving this issue open for the admin.",
                user,
            )
            handled[key] = {"status": "cooldown"}
            logger.info(f"Overseerr replace: issue #{key} hit the cooldown, left open")
            return

        try:
            label, item = self._replace(item_id, issue["id"])
        except Exception as e:
            handled[key] = {"status": "error"}
            logger.warning(f"Overseerr replace: couldn't replace for issue #{key}: {e}")
            return

        if item is None:
            handled[key] = {"status": "unknown"}
            return

        replaced[str(item_id)] = now.timestamp()
        handled[key] = {
            "status": "replacing",
            "item_id": item_id,
            "label": label,
            "started": now.timestamp(),
        }
        api.comment_issue(
            issue["id"],
            f"Finding another copy of {label}. If it was playing, playback was "
            "stopped; it stays in the library and is swapped to the new copy. This "
            "issue resolves itself once the new copy is in.",
            user,
        )
        logger.info(f"Overseerr replace: issue #{key} → replacing {label}")
        self._notify(issue, label, item_id)

    def _follow_up(
        self,
        api: OverseerrAPI,
        handled: dict[str, dict[str, Any]],
        open_ids: set[str],
        user: int,
    ) -> None:
        """Comment on and resolve issues whose replace finished."""

        progress = dict[int, tuple[str, str]]()

        for key, entry in list(handled.items()):
            if entry["status"] != "replacing":
                continue

            if key not in open_ids:
                # Resolved or deleted in Overseerr meanwhile
                del handled[key]
                continue

            item_id = entry["item_id"]

            if item_id not in progress:
                progress[item_id] = self._progress(item_id)

            outcome, release = progress[item_id]

            if outcome == "waiting":
                continue

            issue_id = int(key)

            if outcome == "done":
                note = f"New copy is in: {release}." if release else "New copy is in."

                if api.comment_issue(issue_id, note, user):
                    api.resolve_issue(issue_id, user)

                logger.info(f"Overseerr replace: resolved #{key}, {entry['label']} replaced")
            elif outcome == "failed":
                api.comment_issue(
                    issue_id,
                    "Riven couldn't find another copy. Leaving this open for the admin.",
                    user,
                )
                logger.info(f"Overseerr replace: no new copy for {entry['label']} (#{key})")
            else:
                if api.comment_issue(issue_id, "Removed from Riven, closing.", user):
                    api.resolve_issue(issue_id, user)

            handled[key] = {"status": outcome}

    def _notify(self, issue: dict[str, Any], label: str, item_id: int) -> None:
        if not self.settings.notify_urls:
            return

        reporter = issue.get("createdBy") or {}
        name = reporter.get("displayName") or reporter.get("username") or "Someone"
        message = next(
            (c.get("message") for c in issue.get("comments") or [] if c.get("message")),
            "",
        )
        notice = Notice(
            event="Issue Reported: Replacing",
            title=label,
            description=message,
            color=PURPLE,
            fields=[("Reported By", name), ("Issue", f"#{issue['id']}")],
        )

        try:
            with db_session() as session:
                item = session.get(MediaItem, item_id)

                if item is not None:
                    notice.with_item(
                        item, settings_manager.settings.notifications.link_url
                    )

                send(self.settings.notify_urls, notice)
        except Exception as e:
            logger.warning(f"Overseerr replace: notification failed: {e}")


overseerr_replace_service = OverseerrReplace()


def overseerr_replace() -> None:
    """Scheduled entry point."""

    overseerr_replace_service.run()
