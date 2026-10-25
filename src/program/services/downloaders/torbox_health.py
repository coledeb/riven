"""
TorBox health: hold known-good files while TorBox is degraded
(``downloaders.torbox.degraded_hold``), and follow TorBox's status page
(``downloaders.torbox.status_page``).

TorBox counts as degraded when ``failures`` API calls failed (timeouts,
connection errors, HTTP 5xx; not 429 or a confirmed "not found") within
``window_minutes``, until ``clear_minutes`` after the last one. (The API
circuit breaker alone doesn't count: it also trips on 429 rate limits.)

Riven's own failures always decide; the status page (``use_status_page``) only
makes it react sooner, since status pages lag (both ways). API or Database not
operational on the page: a single own failure is enough. A CDN region not
operational on the page: files on that region's servers
(``nexus-238.cnam.tb-cdn.io`` -> CNAM) are held once Riven itself sees a
streaming failure there. Either way the hold ends ``clear_minutes`` after the
last own failure, whatever the page still says; a stale or unreadable page is
ignored.

An independent uptime monitor (``status_page.monitor_url``, IbbyLabs by
default) is a second page read the same way: its TorBox API probe counts like
API trouble, its TorBox Streaming probe like every CDN region being down. Its
other probes (website, webhooks, usenet, ...) don't affect Riven and are ignored.

While degraded, the places that would replace, blacklist or give up on
something because of a TorBox error ask ``hold()`` first and skip it: slow
source replacement, dead-link re-sourcing, download blacklisting, uncached
download give-ups, and treating torrents missing from the account listing as
deleted. Nothing is queued: the normal checks pick things up after recovery.

The status page's incidents and maintenance can also be posted (``notify``):
when one starts, every update, and when it's resolved.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from loguru import logger

from program.settings import settings_manager
from program.utils import data_dir_path
from program.utils.notify import BLUE, GREEN, ORANGE, RED, Notice, send

PROVIDER = "torbox"

# Status page resources whose trouble holds everything (the rest are other
# services, or CDN regions that only hold files on their own servers).
GLOBAL_RESOURCES = {"api", "database"}
OK_STATES = {"operational"}

# Uptime monitor (IbbyLabs /v1/status): service id, and its probes that matter:
# probe id -> name for messages. Streaming isn't per region, so it's "*" (any).
MONITOR_SERVICE = "torbox-app"
MONITOR_API_PROBES = {"torbox": "API"}
MONITOR_STREAM_PROBES = {"torbox-streaming"}
MONITOR_STATES = {"DOWN": "downtime", "DEGRADED": "degraded", "MAINTENANCE": "maintenance"}
ANY_REGION = "*"

# "nexus-238.cnam.tb-cdn.io" -> "cnam"
REGION_HOST = re.compile(r"\.([a-z0-9]+)\.tb-cdn\.io$", re.IGNORECASE)

# Degraded/recovered notices: a new "degraded" notice at most this often (flapping)
NOTICE_GAP_SECONDS = 30 * 60
PAGE_TIMEOUT = 10

# Plain names for notices: "torrents/mylist: Timeout" -> "the torrent list timed out"
_CALLS = {
    "user/me": "the account check",
    "torrents/mylist": "the torrent list",
    "torrents/torrentinfo": "a torrent lookup",
    "torrents/createtorrent": "adding a torrent",
    "torrents/checkcached": "a cache check",
    "torrents/controltorrent": "a torrent change",
    "torrents/requestdl": "a stream link",
    "requestdl": "a stream link",
}


def _plain_failure(what: str) -> str:
    endpoint, _, error = what.partition(": ")
    call = _CALLS.get(endpoint.strip("/"), endpoint)

    if "Timeout" in error:
        return f"{call} timed out"
    if error.startswith("HTTP"):
        return f"{call} got a server error ({error})"
    if "Connection" in error:
        return f"{call} couldn't connect"

    return f"{call}: {error}" if error else call


def region_of(host: str | None) -> str | None:
    """The TorBox CDN region code of a host, lower case, or None."""

    match = REGION_HOST.search(host or "")
    return match[1].lower() if match else None


@dataclass
class PageStatus:
    """What the status page said at one check."""

    checked_at: float
    # Global resources (API, Database) not operational: name -> status
    problems: dict[str, str] = field(default_factory=dict)
    # CDN regions not operational: region code -> status
    regions: dict[str, str] = field(default_factory=dict)
    # Open incident and ongoing maintenance titles, for messages
    incidents: list[str] = field(default_factory=list)
    # Their pages: title -> URL
    incident_urls: dict[str, str] = field(default_factory=dict)


class TorBoxHealth:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # (when, dedupe key or None); one torrent failing over and over counts once
        self._failures = deque[tuple[float, str | None]]()
        # When TorBox API calls answered fine, to tell a few bad files from TorBox failing
        self._successes = deque[float]()
        self._own_until = 0.0
        self._own_reason = ""
        self._own_count = 0
        self._last_failure = ""
        # CDN region -> when Riven last saw a streaming failure on its servers
        self._cdn_failed = dict[str, float]()
        # Last read of each page: "status_page" (TorBox's own) / "monitor"
        self._pages: dict[str, PageStatus] = {}
        # Notices
        self._announced = False
        self._announced_at = -NOTICE_GAP_SECONDS
        self._since: float | None = None
        self._held_count = 0

    @property
    def settings(self):
        return settings_manager.settings.downloaders.torbox.degraded_hold

    @property
    def page_settings(self):
        return settings_manager.settings.downloaders.torbox.status_page

    # --- signals ---

    def record_failure(
        self, what: str, torrent_id: str | int | None = None, once: str | None = None
    ) -> None:
        """
        A TorBox API call failed (timeout, connection error, HTTP 5xx). Pass the
        torrent for calls about one torrent: repeated failures for the same
        torrent count once, since that's more likely the torrent than TorBox.
        `once` does the same for other repeats of one check (e.g. the startup
        plan check, run again when settings reload).
        """

        torrent = str(torrent_id) if torrent_id is not None else None
        key = f"torrent {torrent}" if torrent else once

        with self._lock:
            self._failures.append((self._clock(), key))
            self._last_failure = what
            self._evaluate()
            counted, share = self._counted(self._clock())

        logger.warning(
            f"TorBox call failed: {what}"
            + (f" (torrent {torrent})" if torrent else "")
            + f"; {counted} counted in {self.settings.window_minutes} min, "
            f"{share:.0%} of calls"
        )

    def record_success(self) -> None:
        """A TorBox API call answered (not a 5xx)."""

        with self._lock:
            self._successes.append(self._clock())

            # Bounded: only the window matters
            while len(self._successes) > 10_000:
                self._successes.popleft()

    def record_cdn_failure(self, host: str | None) -> None:
        """Streaming from a TorBox CDN server failed (timeout, connection error, 5xx)."""

        region = region_of(host)

        if region:
            with self._lock:
                self._cdn_failed[region] = self._clock()

    def set_page(self, page: PageStatus | None, source: str = "status_page") -> None:
        with self._lock:
            if page is not None:
                self._pages[source] = page
                # A failure seen just before the page reported trouble counts
                self._evaluate()

    def _evaluate(self) -> None:
        """Turn degraded on (or extend it) from own failures; call with the lock held."""

        settings = self.settings
        now = self._clock()

        self._trim(now)

        if not self._failures:
            return

        page = self._fresh_page(now)
        last = self._failures[-1][0]
        already = self._own_until > now
        counted, share = self._counted(now)

        if page and page.problems:
            # The page reporting API/Database trouble lowers the bar to one failure
            trips = True
        else:
            trips = counted >= settings.failures and share >= settings.min_failure_share

        if already or trips:
            self._own_until = max(self._own_until, last + settings.clear_minutes * 60)

            if not already:
                self._own_count = counted
                self._own_reason = (
                    f"{counted} TorBox API call(s) failed in "
                    f"{settings.window_minutes} min, {share:.0%} of calls "
                    f"(last: {self._last_failure})"
                )

    def _trim(self, now: float) -> None:
        window = self.settings.window_minutes * 60

        while self._failures and now - self._failures[0][0] > window:
            self._failures.popleft()

        while self._successes and now - self._successes[0] > window:
            self._successes.popleft()

    def _counted(self, now: float) -> tuple[int, float]:
        """Failures in the window (each torrent / `once` key once) and their share of all calls."""

        self._trim(now)
        keys = {k for _, k in self._failures if k is not None}
        counted = sum(1 for _, k in self._failures if k is None) + len(keys)
        calls = len(self._failures) + len(self._successes)
        return counted, (len(self._failures) / calls if calls else 0.0)

    # --- state ---

    def _fresh_page(self, now: float) -> PageStatus | None:
        """What the fresh pages say together (None if no page is fresh or in use)."""

        if not self.settings.use_status_page:
            return None

        urls = {"status_page": self.page_settings.url, "monitor": self.page_settings.monitor_url}
        # Ignore a page we haven't managed to read for a while
        max_age = max(3 * self.page_settings.interval_minutes * 60, 15 * 60)
        fresh = [
            page
            for source, page in self._pages.items()
            if urls.get(source) and now - page.checked_at <= max_age
        ]

        if len(fresh) <= 1:
            return fresh[0] if fresh else None

        merged = PageStatus(checked_at=max(p.checked_at for p in fresh))

        for page in fresh:
            merged.problems.update(page.problems)
            merged.regions.update(page.regions)
            merged.incidents += page.incidents
            merged.incident_urls.update(page.incident_urls)

        return merged

    def degraded(self) -> bool:
        """TorBox as a whole is degraded (decided by own failures; see above)."""

        if not self.settings.enabled:
            return False

        with self._lock:
            return self._own_until > self._clock()

    def holding(self, host: str | None = None, provider: str = PROVIDER) -> bool:
        """True if TorBox (or the CDN region of `host`) is degraded right now."""

        if provider != PROVIDER or not self.settings.enabled:
            return False

        if self.degraded():
            return True

        region = region_of(host)

        if not region:
            return False

        settings = self.settings
        now = self._clock()

        with self._lock:
            page = self._fresh_page(now)
            failed = self._cdn_failed.get(region)
            # The page says the region is down, and Riven saw it fail recently
            return bool(
                page
                and (region in page.regions or ANY_REGION in page.regions)
                and failed is not None
                and now - failed <= settings.clear_minutes * 60
            )

    def hold(
        self,
        what: str,
        host: str | None = None,
        provider: str = PROVIDER,
        offline_server: bool = False,
    ) -> bool:
        """
        Whether to skip a destructive action because TorBox is degraded. If so,
        it's logged and listed in the "recovered" notice.

        offline_server: TorBox gave a link on a server it has pulled (0.0.0.0).
        That's TorBox saying the file can't be streamed, so it's only held when
        the status page reports TorBox or that server's region down (TorBox-wide
        maintenance pulls every server; re-sourcing then would be pointless).
        """

        if offline_server:
            if not self._offline_server_held(host, provider):
                return False
        elif not self.holding(host, provider):
            return False

        logger.info(f"TorBox degraded ({self.reason(host)}); kept {what}")

        with self._lock:
            self._held_count += 1

        return True

    def _offline_server_held(self, host: str | None, provider: str) -> bool:
        if provider != PROVIDER or not self.settings.enabled:
            return False

        with self._lock:
            page = self._fresh_page(self._clock())

        region = region_of(host)
        return bool(
            page
            and (
                page.problems
                or (region and (region in page.regions or ANY_REGION in page.regions))
            )
        )

    def reason(self, host: str | None = None) -> str:
        """Why TorBox counts as degraded, for messages."""

        now = self._clock()
        reasons = list[str]()

        with self._lock:
            if self._own_until > now:
                reasons.append(self._own_reason)

            page = self._fresh_page(now)

            if page:
                if page.problems:
                    parts = ", ".join(f"{k} {v}" for k, v in page.problems.items())
                    reasons.append(f"status page: {parts}")

                region = region_of(host)

                if region and region in page.regions:
                    reasons.append(
                        f"status page: {region.upper()} CDN {page.regions[region]}"
                    )
                elif region and ANY_REGION in page.regions:
                    reasons.append(f"uptime monitor: streaming {page.regions[ANY_REGION]}")

                if reasons and page.incidents:
                    reasons.append(f"incident: {'; '.join(page.incidents)}")

        return "; ".join(reasons) or "unknown"

    def summary(self) -> tuple[str, list[tuple[str, str]]]:
        """Notice title and fields saying in plain words why TorBox counts as degraded."""

        now = self._clock()
        fields = list[tuple[str, str]]()

        with self._lock:
            count, last = self._own_count, _plain_failure(self._last_failure)
            page = self._fresh_page(now)

        if count <= 1:
            seen = f"A TorBox request failed: {last}."
        else:
            seen = (
                f"{count} TorBox requests failed within "
                f"{self.settings.window_minutes} min; the last: {last}."
            )

        fields.append(("What Riven saw", seen))
        maintenance = False

        if page and page.problems:
            by_state = dict[str, list[str]]()

            for name, state in page.problems.items():
                by_state.setdefault(_label(state), []).append(name)

            lines = [f"{' and '.join(names)}: {state}" for state, names in by_state.items()]
            lines += [
                f"[{title}]({page.incident_urls[title]})"
                if title in page.incident_urls
                else title
                for title in page.incidents
            ]
            fields.append(("TorBox status page", "\n".join(lines)))
            maintenance = set(page.problems.values()) == {"maintenance"}

        title = "TorBox is down for maintenance" if maintenance else "TorBox is having problems"
        return title, fields

    # --- scheduled check ---

    def check(self) -> None:
        """Read the status page when due, and announce degraded / recovered."""

        try:
            status_page.run()
        except Exception as e:
            logger.debug(f"TorBox status page: check failed: {e}")

        if not self.settings.enabled:
            return

        degraded = self.degraded()
        now = self._clock()

        if not degraded and self._since is None:
            # Only CDN-region holds (logged as they happen); nothing to report
            with self._lock:
                self._held_count = 0

            return

        if degraded and self._since is None:
            self._since = now
            reason = self.reason()
            logger.warning(f"TorBox is degraded ({reason}); holding files")

            if now - self._announced_at >= NOTICE_GAP_SECONDS:
                self._announced = True
                self._announced_at = now
                title, fields = self.summary()
                self._notify(
                    Notice(
                        event="TorBox Degraded",
                        title=title,
                        description=(
                            "Riven is keeping your current files: nothing gets "
                            "replaced, blacklisted or removed because of TorBox "
                            "errors until it's back. Playback and new downloads "
                            "may fail meanwhile. You'll get a message when it recovers."
                        ),
                        color=ORANGE,
                        fields=fields,
                        url=self.page_settings.url or None,
                    )
                )
        elif not degraded and self._since is not None:
            minutes = round((now - self._since) / 60)

            with self._lock:
                count, self._held_count = self._held_count, 0

            logger.info(
                f"TorBox recovered after {minutes} min; {count} action(s) were held"
            )

            if self._announced:
                # Each held action is in the log; the notice only counts them
                held = (
                    f"Riven held off on {count} action{'s' if count != 1 else ''} "
                    "(replacing or removing files); the normal checks pick them up now."
                    if count
                    else "Riven didn't have to hold off on anything."
                )

                self._notify(
                    Notice(
                        event="TorBox Recovered",
                        title="TorBox is back",
                        description=f"It had problems for about {minutes} min. {held}",
                        color=GREEN,
                        url=self.page_settings.url or None,
                    )
                )

            self._since = None
            self._announced = False

    def _notify(self, notice: Notice) -> None:
        if self.settings.notify_urls:
            send(self.settings.notify_urls, notice)


# --- status page ---

_STATE_LABEL = {
    "operational": "Operational",
    "degraded": "Degraded",
    "downtime": "Downtime",
    "maintenance": "Maintenance",
    "resolved": "Resolved",
}


def _time(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _label(state: str | None) -> str:
    return _STATE_LABEL.get(state or "", (state or "unknown").capitalize())


@dataclass
class Report:
    """One incident or maintenance from the status page."""

    id: str
    title: str
    maintenance: bool
    state: str
    starts_at: datetime | None
    resolved: bool
    affected: list[str]
    # (id, published_at, message, state), oldest first
    updates: list[tuple[str, datetime | None, str, str]]


class TorBoxStatusPage:
    """Reads TorBox's status page; feeds TorBoxHealth and posts incidents."""

    def __init__(
        self,
        get: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.time,
        state_file=None,
    ) -> None:
        self._get = get or (lambda url: requests.get(url, timeout=PAGE_TIMEOUT))
        self._clock = clock
        self._state_file = state_file or data_dir_path / "torbox_status.json"
        self._last_run = 0.0
        self._lock = threading.Lock()
        self._state: dict[str, Any] | None = None

    @property
    def settings(self):
        return settings_manager.settings.downloaders.torbox.status_page

    def run(self) -> None:
        settings = self.settings
        hold = settings_manager.settings.downloaders.torbox.degraded_hold

        signal = hold.enabled and hold.use_status_page
        # The monitor is only a degraded signal; TorBox's page also feeds notify
        official = bool(settings.url) and (settings.notify or signal)
        monitor = bool(settings.monitor_url) and signal

        if not official and not monitor:
            return

        now = self._clock()

        if now - self._last_run < settings.interval_minutes * 60 - 5:
            return

        if not self._lock.acquire(blocking=False):
            return

        try:
            self._last_run = now
            error: Exception | None = None

            if official:
                try:
                    response = self._get(f"{settings.url.rstrip('/')}/index.json")
                    response.raise_for_status()
                    data = response.json()
                    health.set_page(self.parse(data, now))
                    self.announce(data)
                except Exception as e:
                    error = e

            if monitor:
                try:
                    response = self._get(settings.monitor_url)
                    response.raise_for_status()
                    health.set_page(self.parse_monitor(response.json(), now), "monitor")
                except Exception as e:
                    error = error or e

            if error:
                raise error
        finally:
            self._lock.release()

    # --- parsing ---

    @staticmethod
    def _included(data: dict, kind: str) -> dict[str, dict]:
        return {
            str(x.get("id")): x
            for x in data.get("included") or []
            if x.get("type") == kind
        }

    def parse(self, data: dict, now: float) -> PageStatus:
        page = PageStatus(checked_at=now)

        for resource in self._included(data, "status_page_resource").values():
            attributes = resource.get("attributes") or {}
            name = str(attributes.get("public_name") or "")
            state = str(attributes.get("status") or "operational").lower()

            if state in OK_STATES:
                continue

            if name.lower() in GLOBAL_RESOURCES:
                page.problems[name] = state
            elif name.upper().endswith(" CDN"):
                page.regions[name[:-4].strip().lower()] = state

        started = datetime.fromtimestamp(now, timezone.utc)
        ongoing = [
            r
            for r in self.reports(data)
            if not r.resolved
            and (not r.maintenance or not r.starts_at or r.starts_at <= started)
        ]
        page.incidents = [r.title for r in ongoing]
        page.incident_urls = {r.title: self._report_url(r) for r in ongoing}
        return page

    @staticmethod
    def parse_monitor(data: dict, now: float) -> PageStatus | None:
        """The uptime monitor's TorBox API / Streaming probes (None if its data is stale)."""

        if (data.get("freshness") or {}).get("stale"):
            return None

        page = PageStatus(checked_at=now)
        service = next(
            (s for s in data.get("services") or [] if s.get("id") == MONITOR_SERVICE),
            None,
        )

        if service is None:
            return page

        for probe in service.get("probes") or []:
            pid = probe.get("id")
            state = MONITOR_STATES.get(str(probe.get("state") or "").upper())

            if state is None:
                continue

            if probe.get("officialMaintenance") or service.get("maintenance"):
                state = "maintenance"

            if pid in MONITOR_API_PROBES:
                page.problems[f"{MONITOR_API_PROBES[pid]} (uptime monitor)"] = state
            elif pid in MONITOR_STREAM_PROBES:
                page.regions[ANY_REGION] = state

        return page

    def reports(self, data: dict) -> list[Report]:
        resources = {
            key: (r.get("attributes") or {}).get("public_name") or key
            for key, r in self._included(data, "status_page_resource").items()
        }
        updates = self._included(data, "status_update")
        reports = list[Report]()

        for key, report in self._included(data, "status_report").items():
            attributes = report.get("attributes") or {}
            state = str(attributes.get("aggregate_state") or "").lower()
            ends_at = _time(attributes.get("ends_at"))
            resolved = state == "resolved" or bool(
                ends_at and ends_at <= datetime.now(timezone.utc)
            )
            ids = [
                str(u.get("id"))
                for u in ((report.get("relationships") or {}).get("status_updates") or {}).get("data") or []
            ]
            items = list[tuple[str, datetime | None, str, str]]()

            for uid in ids:
                update = (updates.get(uid) or {}).get("attributes") or {}
                worst = _worst(update.get("affected_resources") or [])
                items.append(
                    (uid, _time(update.get("published_at")), str(update.get("message") or ""), worst)
                )

            items.sort(key=lambda u: u[1] or datetime.min.replace(tzinfo=timezone.utc))
            reports.append(
                Report(
                    id=key,
                    title=str(attributes.get("title") or "TorBox incident"),
                    maintenance=attributes.get("report_type") == "maintenance",
                    state=state,
                    starts_at=_time(attributes.get("starts_at")),
                    resolved=resolved,
                    affected=[
                        str(resources.get(str(a.get("status_page_resource_id")), ""))
                        for a in attributes.get("affected_resources") or []
                        if resources.get(str(a.get("status_page_resource_id")))
                    ],
                    updates=items,
                )
            )

        reports.sort(key=lambda r: r.starts_at or datetime.min.replace(tzinfo=timezone.utc))
        return reports

    # --- posting ---

    def _report_url(self, report: Report) -> str:
        kind = "maintenance" if report.maintenance else "incident"
        return f"{self.settings.url.rstrip('/')}/{kind}/{report.id}"

    def _load(self) -> dict[str, Any] | None:
        if self._state is None:
            try:
                self._state = json.loads(self._state_file.read_text())
            except FileNotFoundError:
                return None
            except Exception as e:
                logger.warning(f"TorBox status page: ignoring unreadable {self._state_file}: {e}")
                return None

        return self._state

    def _save(self, state: dict[str, Any]) -> None:
        self._state = state

        try:
            self._state_file.write_text(json.dumps(state, sort_keys=True))
        except Exception as e:
            logger.warning(f"TorBox status page: couldn't save {self._state_file}: {e}")

    def announce(self, data: dict) -> None:
        """
        Post what changed since the last check: a new incident, each new
        update, and the resolution. Everything is tracked even with notify off,
        so turning it on doesn't post the page's history; ongoing incidents are
        posted once when notify is first turned on.
        """

        notify = self.settings.notify and bool(self.settings.notify_urls)
        reports = self.reports(data)
        logo = ((data.get("data") or {}).get("attributes") or {}).get("logo_url")
        previous = self._load()
        seen = (previous or {}).get("reports", {})
        # First check ever, or notify was off last time: don't post history
        first_look = previous is None or (notify and not previous.get("notify"))
        state: dict[str, Any] = {"notify": notify, "reports": {}}
        posts = list[Notice]()

        for report in reports:
            before = seen.get(report.id)
            state["reports"][report.id] = {
                "updates": [u[0] for u in report.updates],
                "resolved": report.resolved,
            }

            if first_look:
                # Only what's ongoing, once, as it stands now
                if not report.resolved:
                    latest = report.updates[-1] if report.updates else None
                    posts.append(self._notice(report, latest, "start", logo))
                continue

            known = set(before["updates"]) if before else set()
            new = [u for u in report.updates if u[0] not in known]
            newly_resolved = report.resolved and not (before or {}).get("resolved")

            if before is None and not new:
                # A new report without any update yet
                posts.append(
                    self._notice(report, None, "resolved" if report.resolved else "start", logo)
                )
                continue

            for index, update in enumerate(new):
                first = before is None and index == 0
                last = index == len(new) - 1

                if last and newly_resolved:
                    kind = "start_resolved" if first else "resolved"
                else:
                    kind = "start" if first else "update"

                posts.append(self._notice(report, update, kind, logo))

            if newly_resolved and not new:
                posts.append(self._notice(report, None, "resolved", logo))

        self._save(state)

        if notify:
            for notice in posts:
                send(self.settings.notify_urls, notice)

    def _notice(
        self,
        report: Report,
        update: tuple[str, datetime | None, str, str] | None,
        kind: str,
        logo: str | None,
    ) -> Notice:
        noun = "Maintenance" if report.maintenance else "Incident"
        event = {
            "start": f"TorBox {noun}",
            "update": f"TorBox {noun} Update",
            "resolved": f"TorBox {noun} Resolved",
            "start_resolved": f"TorBox {noun} (Resolved)",
        }[kind]
        if update and update[2]:
            message = update[2]
        elif kind in ("resolved", "start_resolved"):
            message = f"TorBox marked this {noun.lower()} resolved."
        else:
            message = f"TorBox posted a new {noun.lower()}."
        state = "resolved" if kind in ("resolved", "start_resolved") else (update[3] if update and update[3] else report.state)

        if kind in ("resolved", "start_resolved"):
            color = GREEN
        elif report.maintenance or state == "maintenance":
            color = BLUE
        elif state == "downtime":
            color = RED
        else:
            color = ORANGE

        fields = [("Status", _label(state))]

        if report.affected:
            fields.append(("Affected", ", ".join(report.affected)))

        if report.starts_at:
            fields.append(("Started", report.starts_at.astimezone().strftime("%b %d, %H:%M %Z")))

        return Notice(
            event=event,
            title=report.title,
            description=message,
            color=color,
            fields=fields,
            url=self._report_url(report),
            image=logo,
            timestamp=update[1] if update and update[1] else None,
        )


_SEVERITY = ["operational", "maintenance", "degraded", "downtime"]


def _worst(affected: list[dict]) -> str:
    states = [str(a.get("status") or "").lower() for a in affected]
    known = [s for s in states if s in _SEVERITY]
    return max(known, key=_SEVERITY.index) if known else ""


health = TorBoxHealth()
status_page = TorBoxStatusPage()


def torbox_health_check() -> None:
    """Scheduled every minute: status page when due, degraded/recovered notices."""

    health.check()
