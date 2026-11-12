"""
Scheduled cleanup of unused TorBox torrents (``downloaders.torbox.autoclean``).

Once a day or once a week, at a chosen half hour, every torrent on the account
that no Riven file uses is deleted: also those Riven didn't add, such as copies
added in Debrid Media Manager and older copies left behind after a replace.
Kept: torrents any stored file links to, uncached downloads still running,
and torrents younger than ``min_age_hours``. Each run's deleted torrents are
saved to ``data/torbox_autoclean/deleted-<time>.json`` (re-add by hash).

A tick runs every minute and reads the settings live, so the schedule can be
changed without a restart. A run missed while Riven was down (or TorBox was
degraded) is caught up within ``CATCH_UP``; turning the schedule on never
runs a slot that had already passed.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

from program.settings import settings_manager
from program.utils import data_dir_path

if TYPE_CHECKING:
    from .torbox import TorBoxDownloader

STATE_FILE = "torbox_autoclean.json"
BACKUP_DIR = "torbox_autoclean"
CATCH_UP = timedelta(hours=6)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def last_slot(now: datetime, schedule: str, time: str, weekday: str) -> datetime | None:
    """The latest scheduled run time at or before ``now`` (None when off)."""

    if schedule not in ("daily", "weekly"):
        return None

    hour, minute = (int(part) for part in time.split(":"))
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if schedule == "daily":
        return slot if slot <= now else slot - timedelta(days=1)

    slot -= timedelta(days=(now.weekday() - WEEKDAYS.index(weekday)) % 7)

    return slot if slot <= now else slot - timedelta(days=7)


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


class AutoClean:
    def __init__(self, torbox: "TorBoxDownloader") -> None:
        self.torbox = torbox
        self._lock = threading.Lock()
        self._state_file = data_dir_path / STATE_FILE
        self._state = self._load()

    @property
    def settings(self):
        return settings_manager.settings.downloaders.torbox.autoclean

    # --- state ---

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"TorBox autoclean: ignoring unreadable {self._state_file}: {e}")
            return {}

    def _save(self) -> None:
        try:
            self._state_file.write_text(json.dumps(self._state, indent=1))
        except Exception as e:
            logger.warning(f"TorBox autoclean: couldn't save {self._state_file}: {e}")

    # --- schedule ---

    def due(self, now: datetime) -> datetime | None:
        """The slot to run now, if one is due; arms/disarms on schedule changes."""

        s = self.settings
        key = " ".join(
            [s.schedule] + ([s.weekday] if s.schedule == "weekly" else []) + [s.time]
        )

        if s.schedule == "off":
            if self._state.get("armed"):
                self._state.pop("armed", None)
                self._state.pop("schedule", None)
                self._save()
            return None

        # Newly turned on or moved: only slots after this moment count
        if not self._state.get("armed") or self._state.get("schedule") != key:
            self._state.update(armed=now.isoformat(), schedule=key)
            self._save()
            logger.info(f"TorBox autoclean scheduled: {key}")
            return None

        slot = last_slot(now, s.schedule, s.time, s.weekday)
        done = max(
            filter(None, [_parse(self._state.get("armed")), _parse(self._state.get("last_run"))])
        )

        if slot is None or slot <= done or now - slot > CATCH_UP:
            return None

        return slot

    def tick(self) -> None:
        """Run once per minute from the scheduler."""

        if not self._lock.acquire(blocking=False):
            return

        try:
            now = datetime.now().astimezone()
            slot = self.due(now)

            if slot is None:
                return

            from .torbox_health import health

            if health.degraded():
                # A short account listing would look like nothing to clean; retry later
                logger.debug("TorBox autoclean: waiting, TorBox is degraded")
                return

            self.run(reason=f"scheduled {slot:%a %H:%M}")
            self._state["last_run"] = now.isoformat()
            self._save()
        except Exception as e:
            logger.warning(f"TorBox autoclean failed, will retry: {e}")
        finally:
            self._lock.release()

    # --- cleanup ---

    def candidates(self, now: datetime) -> list[dict[str, Any]]:
        """Account torrents that are unused and old enough to delete."""

        account = list(self.torbox._iter_account())
        used = self.torbox._used_ids() | self.torbox.uncached.torrent_ids()
        cutoff = now - timedelta(hours=self.settings.min_age_hours)
        out = []

        for t in account:
            tid = str(t.get("id") or "")
            created = _parse(str(t.get("created_at") or "").replace("Z", "+00:00"))

            if not tid or tid in used or created is None or created > cutoff:
                continue

            out.append(t)

        return out

    def run(self, reason: str = "manual") -> list[dict[str, Any]]:
        """Delete every unused torrent now; returns the deleted ones."""

        now = datetime.now().astimezone()
        unused = self.candidates(now)

        if not unused:
            logger.info(f"TorBox autoclean ({reason}): nothing unused")
            return []

        keep = ("id", "hash", "name", "size", "created_at")
        listed = [{k: t.get(k) for k in keep} for t in unused]
        backup = data_dir_path / BACKUP_DIR / f"deleted-{now:%Y%m%d-%H%M}.json"

        try:
            backup.parent.mkdir(exist_ok=True)
            backup.write_text(json.dumps(listed, indent=1))
        except Exception as e:
            # Without the list the deletions couldn't be undone: stop here
            raise RuntimeError(f"couldn't save {backup}: {e}") from e

        deleted, failed = [], 0

        for t in listed:
            try:
                if self.torbox.delete_torrent(t["id"], include_foreign=True, cleanup=True):
                    deleted.append(t)
            except Exception as e:
                failed += 1
                logger.debug(f"TorBox autoclean: failed to delete {t['id']}: {e}")

        gb = sum(t.get("size") or 0 for t in deleted) / 1e9

        logger.info(
            f"TorBox autoclean ({reason}): deleted {len(deleted)} unused torrent(s), "
            f"{gb:.1f} GB" + (f", {failed} failed" if failed else "") + f"; list in {backup}"
        )
        self._notify(deleted, gb, failed)

        return deleted

    def _notify(self, deleted: list[dict[str, Any]], gb: float, failed: int) -> None:
        urls = self.settings.notify_urls

        if not urls or not deleted:
            return

        from program.utils.notify import GREEN, Notice, send

        names = [str(t.get("name") or t["id"])[:80] for t in deleted]
        shown = "\n".join(names[:15]) + (f"\n…and {len(names) - 15} more" if len(names) > 15 else "")
        fields = [("Freed", f"{gb:.1f} GB")]

        if failed:
            fields.append(("Failed", str(failed)))

        send(
            urls,
            Notice(
                event="torbox_autoclean",
                title=f"TorBox Autoclean: {len(deleted)} unused torrent(s) deleted",
                description=shown,
                color=GREEN,
                fields=fields,
            ),
        )
