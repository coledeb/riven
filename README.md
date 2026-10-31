<div align="center">
  <a href="https://github.com/rivenmedia/riven">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/rivenmedia/riven/main/assets/riven-light.png">
      <img alt="riven" src="https://raw.githubusercontent.com/rivenmedia/riven/main/assets/riven-dark.png">
    </picture>
  </a>
</div>

<div align="center">
  <a href="https://github.com/rivenmedia/riven/stargazers"><img alt="GitHub Repo stars" src="https://img.shields.io/github/stars/rivenmedia/riven"></a>
  <a href="https://github.com/rivenmedia/riven/issues"><img alt="Issues" src="https://img.shields.io/github/issues/rivenmedia/riven" /></a>
  <a href="https://github.com/rivenmedia/riven/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/github/license/rivenmedia/riven"></a>
  <a href="https://github.com/rivenmedia/riven/graphs/contributors"><img alt="Contributors" src="https://img.shields.io/github/contributors/rivenmedia/riven" /></a>
  <a href="https://discord.riven.tv"><img alt="Discord" src="https://img.shields.io/badge/Join%20discord-8A2BE2" /></a>
</div>

<div align="center">
  <p>Plex torrent streaming through Debrid and 3rd party services like Overseerr, Mdblist, etc.</p>
</div>

> [!NOTE]
> **About this fork (TorBox support)**
>
> This fork (re)introduces [TorBox](https://torbox.app) as a supported Debrid service within Riven. Upstream Riven no longer supports TorBox. The downloader is based on the earlier [TorBox-App/riven](https://github.com/TorBox-App/riven) fork and updated for the current downloader interface and RivenVFS.
>
> It was written with TorBox's rate limits in mind, aiming to be a respectful API client that stays well inside them rather than leaning on retries:
>
> - **Cached-only adds.** Hashes are checked with `checkcached` and added with `add_only_if_cached`, so nothing downloads from peers or wastes the hourly add limit (unless uncached downloads are turned on, below).
> - **Uncached downloads as a last resort (optional).** With `uncached.enabled`, when no cached torrent passes the ranking and file rules, TorBox downloads the best one, quality or seeders first (`prefer`; tiers 25+/10+/5+ from Torrentio's counts), only while a plan slot is free (`active_download_slots` 3/5/10); stalled downloads are dropped for the next candidate, and starts are reported via Apprise with the seeder count.
> - **Paced requests.** API calls are paced at 240/min (limit 300) and download-link requests at 15/min, leaving room for other tools on the key.
> - **Back off on 429s.** Rate-limited requests wait out TorBox's `Retry-After` instead of retrying in a loop.
> - **Few link requests.** Links are resolved only when a file is opened, then reused.
> - **Fresh account listings.** `mylist` is read with `bypass_cache=true` and paged.
> - **Shared accounts are safe.** Riven only deletes torrents it created (`only_manage_own_torrents`) and never ones its library still uses.
> - **No leftover torrents.** Torrents Riven added that no file uses any more (replaced, blacklisted or removed) are deleted after 30 minutes unused (`remove_unused_torrents`, `unused_grace_minutes`). Riven remembers which torrents it added across restarts.
> - **Import from the TorBox account (optional).** `account_import` links torrents you add elsewhere (e.g. [Debrid Media Manager](https://debridmediamanager.com)), matched by name including two-language names, to missing movies and episodes; a show not yet in Riven is added with only the imported episodes (the rest paused until requested in Seerr). With `replace_existing` they also replace existing files in place (stopping Plex playback first with `stop_playback_to_replace`), and unmatched ones are reported via Apprise. Deleting a torrent re-sources its items; add the replacement first and that's a swap (re-adding the same torrent works too).
> - **Offline CDN servers.** Links on servers TorBox has taken offline (resolving to `0.0.0.0`) are treated as unavailable, so Riven picks another source.
> - **Expired CDN links.** A `400 Bad Request` from the CDN is treated like a 404: Riven fetches a fresh link once and retries.
> - **Hold files while TorBox is degraded.** After 3 failed API calls (timeouts, connection errors, 5xx; not 429s) in 15 minutes, making up at least 30% of calls (`min_failure_share`), nothing is replaced, blacklisted or given up on because of TorBox errors: slow sources and dead links stay, failed downloads retry every 10 minutes (3 candidates at a time, no uncached starts), uncached downloads keep waiting, and torrents missing from a listing aren't treated as deleted. Failures for the same torrent count once, so one bad torrent (Plex scanning a season on a pulled server) doesn't trip it; each counted failure is logged with its torrent. A link on a pulled CDN server is still re-sourced during a hold (that's TorBox's own answer), unless the status page reports TorBox or that server's region down. It ends 30 minutes after the last failure. TorBox's [status page](https://status.torbox.app) only makes Riven react sooner, never decides on its own (status pages lag): with the API or database reported down, one failed call is enough; with a CDN region reported down, files on its servers are held once Riven sees a streaming failure there. An independent uptime monitor ([IbbyLabs](https://uptime.ibbylabs.dev/?service=torbox-app), checks every few minutes; `status_page.monitor_url`, empty turns it off) counts the same way: its TorBox API probe like the API being down, its Streaming probe like every CDN region being down; its other probes (website, webhooks, usenet...) are ignored, and so is stale data. Optional Discord notice when it starts and ends, with how many actions were held (each one is logged) (`degraded_hold`). A startup account check that times out counts once, however often settings reloads repeat it.
> - **TorBox status page posts (optional, off by default).** `status_page.notify` posts TorBox's incidents and maintenance as embeds linking to the incident: when one starts, every update, and when it's resolved.
> - **Failed listings aren't empty accounts.** A `mylist` answer of `success: false` (seen while TorBox's database was degraded) is an error, not an account with no torrents.
>
> The TorBox limits and API behaviour this work relies on come from the [Debrid Media Manager notes on TorBox limits](https://notes.debridmediamanager.com/providers/torbox-limits/), written alongside [zurg](https://github.com/debridmediamanager/zurg-public). Many thanks to the DMM/zurg project for documenting them.
>
> **Other improvements**
>
> Changes in this fork that aren't strictly part of TorBox support:
>
> - **Debridio scraper (TorBox only).** Gets real torrent names and file lists for Debridio results from TorBox, with uploader tags stripped and Torrentio's title kept for the same torrent.
> - **Torrentio language flags kept.** Flag emojis become words in titles, so language filters catch more dubs.
> - **Replace a file.** `POST /api/v1/items/replace` blacklists the current source of a movie, episode, season or show and finds another copy. The file stays listed (no "unavailable" in Plex) and switches to the new copy; Plex playback of it is stopped first with a message, since it can't be read in between. The fork frontend has a Replace button for it.
> - **Plex on another machine (optional).** `updaters.plex.path_mappings` maps Riven's paths to Plex's.
> - **Replaced versions leave Plex's trash right away (optional).** With `updaters.plex.remove_replaced_versions`, once Plex has scanned a swapped file, the old version it keeps as unavailable is removed instead of waiting for Empty Trash. Only a version whose file is gone from Riven's library, while the item's new file is present, is removed. That check is also the share-down guard (Plex then finds no version at all), so a mass swap that fills the trash is still cleaned. Needs Plex's "Allow media deletion".
> - **Settings changes no longer break the mount.** Saving settings keeps the running RivenVFS mount instead of mounting a second one.
> - **Blacklisting a pack for one episode sticks.** The pack is blacklisted and not picked again.
> - **No library flicker on settings changes.** The VFS tree is rebuilt only when a library profile changes.
> - **Anime audio preference (optional).** `scraping.anime_audio_order` ranks anime by dual / japanese / dubbed audio.
> - **Keys kept out of logs.** Account keys in logged URLs and errors are replaced with `REDACTED`.
> - **Manual scrape works for shows.** No more `DetachedInstanceError` from `GET /api/v1/scrape`.
> - **Re-sourced files don't go missing in Plex.** A file with a dead link stays in place and switches to the new source. Anything playing it in Plex keeps going until the new copy is ready and is stopped with a message right at the swap, so pressing play works at once and no session reads on into a different file. If a read waits 30 s with no new copy, or none turns up, Plex says so instead. A slow file whose fresh link points at a dead server isn't re-sourced for that alone: the link being played still answers.
> - **Playback survives a rate-limited CDN.** A `429` pauses every connection to that server for its `Retry-After`, else 2/4/8/16/30 s growing with each `429` in a row from that server (across reconnects, reset by a good answer), instead of retrying until players give up. Parallel `429`s count once, and while a server is rate limiting only one connection at a time tries it, so waiting reads don't all retry the moment a pause ends. Checking a fresh link asks for 1 byte, and a `429` there keeps the link rather than discarding it.
> - **Airing seasons get their released episodes.** Released episodes are scraped right after their season, not at the daily retry.
> - **Movies wait for their home release.** A movie counts as released on its first digital, physical or TV release (any country, from TMDB), not when it reaches cinemas, so nothing is scraped while only cams and fakes exist. Without a listed date: 60 days after cinemas (`indexer.movie_home_release_fallback_days`); cinema releases over a year old always count. Unreleased movies are re-checked daily, as dates get announced and moved. `indexer.movie_wait_for_home_release` turns it off.
> - **Seerr season requests respected.** A request for seasons 11-13 fetches only those; the other seasons are paused, and a later request for more seasons resumes them. `POST /api/v1/items/pause` with `"remove_files": true` also pauses completed items and removes their files without blacklisting.
> - **Seerr issues for stuck items (optional).** `content.overseerr.issues` opens a Seerr issue for a released item that stays missing and resolves it once available.
> - **Seerr issues replace the reported copy (optional).** With `content.overseerr.replace_issues` on, a user's Seerr issue for a movie, season or episode runs Replace on it (Plex playback stopped with a message, current source blacklisted, another copy found at the same path), comments on the issue, and resolves it once the new copy is in. A whole-show issue gets a comment asking for the season or episode instead. Skipped: issues Riven opened itself or from `user_id`, issues older than when it was turned on, and an item already replaced within `cooldown_hours` (comment, issue left open). `notify_urls` hears about each replace.
> - **Transcoded Plex sessions are recognized.** When playback omits the source filename, library metadata resolves the exact file so slow-source replacement and import playback stops still work.
> - **Slow sources get replaced (optional).** `stream.slow_replace` retries a fresh link for a file that keeps stalling in Plex, then swaps in another copy at the same path. Widespread slowness, or a debrid service that can't hand out a fresh link (API outage), is reported to `notify_urls` instead and nothing is replaced.
> - **Discord notifications as Seerr-style embeds.** Discord webhook URLs (in `notifications.service_urls` and every `notify_urls`) get an embed like Seerr's: event as the author line, linked title, poster thumbnail, inline fields and a colour per outcome. `notifications.link_url` points the title links at your Seerr (`<url>/movie/<tmdb id>`, `/tv/<tmdb id>`); empty links to TMDB. Other URLs still go through Apprise as text.
> - **Completed once, not on every re-source.** A movie or show is announced as completed (log, frontend, notifications) the first time only; a show again only once it has more completed episodes (a new season). Before, a re-sourced file announced its movie again, and the whole show once per episode state change. Kept in `data/announced_completions.json`; what was already completed when it's first created counts as announced.
> - **Only announce what Seerr won't (optional).** With `notifications.skip_seerr_requests` on, Riven's "Now Available" message is skipped for any title that has a Seerr request (anything but declined or 4K-only, which Seerr announces itself). It still goes out for items added another way, e.g. from DMM or a list. If Seerr can't be reached, only items from the Seerr content source are skipped.
> - **Blacklists expire for temporary problems.** Each blacklisted torrent records why: a pulled CDN server, a slow source or a torrent removed from the account comes back after 24 h, not cached or other download errors after 72 h, and wrong files or manual blacklists never come back. A repeat doubles the time, and after `max_strikes` it's permanent (`scraping.blacklist`). Works with every debrid service; only the pulled-server check is TorBox-specific.
> - **Packs aren't wrongly blacklisted when seasons download together.** If a multi-season pack fetched for one season already filled another, that season's remaining candidates are no longer blacklisted as wrong files.
> - **Failed downloads are remembered.** When every candidate torrent fails, their blacklist entries are now saved and the item moves on to re-scraping, instead of retrying the same torrents.
> - **Calendar works.** `GET /api/v1/calendar` no longer returns 500.
> - **Manual re-index is saved.** `POST /api/v1/items/reindex` reported success but never committed, so fresh metadata (release dates, titles) was thrown away.
> - **Overlapping reads don't stall playback.** Samba and kernel readahead keep several reads in flight on one file; each now gets its chunks fetched (lowest first) instead of only the newest, which left the others waiting 30 s and erroring.
> - **A CDN stall mid-file reconnects.** A read timeout, reset or dropped connection while reading the body now reconnects the stream; before, the retry crashed ("generator didn't stop after athrow()") and every later read of that file failed, so Plex skipped to the next episode.
> - **Streams use HTTP/1.1.** Over HTTP/2 every file on one CDN host shared a single connection, so a file Plex paused reading stalled the others (measured: the second file stopped after 11 MB; over HTTP/1.1 it read 300 MB in 2.8 s).
> - **Playback survives a debrid API hiccup.** When refreshing an expired link fails, it's retried after 2 and 4 s (up to 10 s total), and the stream never falls back to the stored provider link (for TorBox an API URL that always answers `422`). The refresh also no longer blocks every other stream while the provider is slow.
> - **One log line per failed read.** A failing stream fails every pending read on that file; the error is logged once per file handle, with a "repeated N more times" count a minute later or when the file is closed (before, hundreds of identical lines). Known stream failures (HTTP errors, refused connections) are one readable line naming the cause, e.g. `HTTP 422 from api.torbox.app/v1/api/torrents/requestdl`, instead of a Python traceback; real bugs still log theirs.
> - **Real stream errors reported.** A failed first connection raises its real error, so the VFS returns the matching errno; a stream closed while still connecting says so instead of trio's `RuntimeError`.
> - **Fresh links are used at once.** A fresh link from the slow-source check is used for the next connection; before, a stream stuck reconnecting kept the old link, so the file was replaced on the old server's stats.
> - **Reads near the end of a file work.** A read from the last chunk into the footer failed with `Chunk end must be greater than or equal to start` (the footer bound mixed two offsets).
> - **Episodes only take torrents from their season.** S15E12 no longer accepts S13E12 torrents.
> - **Plex doesn't re-check every folder.** VFS folders keep their modified time until their contents change.
> - **File times are correct.** VFS file times are read as UTC, no longer hours in the future.
> - **Manual retry and scrape always run.** They replace queued later events (like a downloader cooldown) instead of being ignored.
> - **Missing items return 404.** `GET /api/v1/items/<id>` no longer returns 500 for unknown items.
> - **Service names spelled right in settings.** Real-Debrid, Debrid-Link, AllDebrid, MediaFusion, RARBG.
> - **Live progress shows the right service.** `event_update` and `/api/v1/events` list each job under the service running it.
>
> Multiple of these fixes came from (or were inspired by) upstream Riven [PRs](https://github.com/rivenmedia/riven/pulls).
>
> For full details of each change, see the [commits list](https://github.com/coledeb/riven/commits/main).

Services currently supported:

| Type              | Supported                                                                         |
| ----------------- | --------------------------------------------------------------------------------- |
| Debrid services   | Real Debrid, All Debrid, Debrid-Link, TorBox                                      |
| Content services  | Plex Watchlist, Seerr, Mdblist, Listrr, Trakt                                 |
| Scraping services | AIOStreams, Comet, Debridio, Jackett, Torrentio, Orionoid, Mediafusion, Prowlarr, Zilean, Rarbg |
| Media servers     | Plex, Jellyfin, Emby                                                              |

## Information

This fork was created for my personal use case, allowing the interoperability of [DUMB](https://dumbarr.com/), TorBox, Riven, Torrentio, Debridio, Seerr, Plex, and Debrid Media Manager across WSL2 and Windows.

With this in mind, I have not created specific documentation for installation, configuration, etc. beyond what is written in this README.

I will not remove other already supported services by upstream Riven, and I will be keeping this branch updated alongside upstream. However, I have *not* verified the state or useability of any services outside of my stack or use case.

Please see the [upstream Riven README](https://github.com/rivenmedia/riven#readme) for more information. It *likely* applies to this fork too.

> [!NOTE]
> For DUMB stack users, you will need to change your config for both Riven's Frontend and Backend like so:
> - **Riven Frontend:** Set `repo_owner` to `coledeb`, `branch_enabled` to `true`, and `branch` to `main`. This uses my [Riven Frontend fork](https://github.com/coledeb/riven-frontend) (current upstream Frontend plus a reworked settings page), which is the Frontend this fork is built and tested with.
>   - Updating the Frontend **WILL** break the embedded UI in DUMB, as DUMB currently expects an older pinned version. See the [instructions](https://github.com/coledeb/riven/blob/main/scripts/DUMB.md#1-riven-frontend-in-dumbs-embedded-ui) in this repo's [scripts](https://github.com/coledeb/riven/tree/main/scripts) folder for more information on this, as well as a fix.
>   - In DUMB, set the Frontend's `DATABASE_URL` (e.g. `/data/riven-frontend/riven.db`) and, optionally, `AUTH_SECRET` in the service's environment. DUMB doesn't set them, and its updates replace the app folder, so the fork's default (`data/riven.db` there, with a generated `auth-secret` file beside it) loses users and logins on the next update.
> - **Riven Backend:** Set `repo_owner` to `coledeb`, `repo_name` to `riven`, `branch_enabled` to `true`, and `branch` to `main`.
>   - Riven can lose its settings after a DUMB container restart followed by a Riven update. See the same [instructions](https://github.com/coledeb/riven/blob/main/scripts/DUMB.md#2-riven-backend-loses-its-settings) for more information and the fix.
> - I also recommend setting `auto_update`  to `true`, and `auto_update_mode` to at least `check_only`; I am actively pushing updates as I work on the fork.

I will possibly consider issues or pull requests on this fork, but I offer no guarantee of this.

## License

This project is licensed under the GNU GPLv3 License - see the [LICENSE](LICENSE) file for details
