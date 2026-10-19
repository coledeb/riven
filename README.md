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
> - **Cached-only adds.** Each hash is checked with `checkcached` first, then added with `add_only_if_cached`. Nothing is downloaded from peers, and the hourly add limit isn't spent on uncached torrents.
> - **Paced requests.** General API calls are paced at 240/min, below the documented 300/min, leaving room for other tools sharing the key. Download-link requests (`requestdl`) have their own, much smaller budget and are paced at 15/min.
> - **Back off on 429s.** When TorBox returns a 429, the affected requests are paused for its `Retry-After` period instead of being retried in a loop.
> - **Few link requests.** Links are resolved only when a file is actually opened, then reused.
> - **Fresh account listings.** `mylist` is read with `bypass_cache=true` and paged.
> - **Shared accounts are safe.** By default Riven only deletes torrents it created itself, never ones that were already on the account. Optionally, turn off `downloaders.torbox.only_manage_own_torrents` to also clean up failed torrents that were already there. Torrents Riven's library still uses are never deleted, including a season pack Riven just added that one episode failed on.
> - **Import from the TorBox account (optional).** Turn on `downloaders.torbox.account_import` to link torrents you add with other tools (e.g. [Debrid Media Manager](https://debridmediamanager.com)) into the library. They are matched by name (library first, then TMDB; a two-language name like "Král Tulsy Tulsa King" matches a library title at either end) and only fill missing movies and episodes. Deleting a torrent Riven uses makes it prefer another torrent you added. Optionally, turn on `downloaders.torbox.account_import.replace_existing` so a torrent you add also replaces the files Riven already has for what it contains, even ones Riven had given up on (e.g. a pack whose CDN server was down; to switch back, delete and re-add it). The file keeps its path and switches source. A file being played switches after playback, or, with `stop_playback_to_replace`, Plex playback is stopped with a message (`stop_playback_message`) and the file switches right away (needs Plex Pass). Unmatched torrents can be reported to an Apprise URL such as a Discord webhook.
> - **Offline CDN servers.** TorBox sometimes hands out links on CDN servers it has taken offline (their names resolve to `0.0.0.0`). Such a link is reported as unavailable, so Riven picks another source for that file instead of failing every read.
> - **Expired CDN links.** TorBox's CDN answers an expired link with `400 Bad Request` rather than 403/410. Riven now treats a 400 like a 404: it fetches a fresh link once and retries, instead of failing every read of a file that has been sitting for a while.
>
> The TorBox limits and API behaviour this work relies on come from the [Debrid Media Manager notes on TorBox limits](https://notes.debridmediamanager.com/providers/torbox-limits/), written alongside [zurg](https://github.com/debridmediamanager/zurg-public). Many thanks to the DMM/zurg project for documenting them.
>
> **Other improvements**
>
> Changes in this fork that aren't strictly part of TorBox support:
>
> - **Debridio scraper (TorBox only).** New scraper that gets real torrent names and file lists for Debridio results from TorBox's `checkcached`. Uploader tags like `[EZTVx.to]` are removed from those names, and when Torrentio returns the same torrent its title is kept, so a cached release isn't dropped as a "site" release.
> - **Torrentio language flags kept.** Torrentio's flag emojis are added to titles as words, so language filters catch more dubs.
> - **Plex on another machine (optional).** `updaters.plex.path_mappings` maps Riven's library paths to the paths Plex uses.
> - **Settings changes no longer break the mount.** Saving settings keeps the running RivenVFS mount instead of mounting a second one over it.
> - **Blacklisting a pack for one episode sticks.** An episode playing from a pack now records the pack's blacklist and won't pick it again.
> - **No library flicker on settings changes.** The VFS tree is only rebuilt when a library profile actually changes.
> - **Anime audio preference (optional).** `scraping.anime_audio_order` ranks anime by audio type (dual / japanese / dubbed) in your chosen order.
> - **Keys kept out of logs.** Account keys in logged URLs and errors are replaced with `REDACTED`.
> - **Manual scrape works for shows.** `GET /api/v1/scrape?item_id=<show>` no longer fails with `DetachedInstanceError`.
> - **Re-sourced files don't go missing in Plex.** When a link dies, the file stays in place and is swapped to the new source, and reads wait for the swap.
> - **Playback survives a rate-limited CDN.** A `429` from the streaming server pauses every connection to that server (its `Retry-After`, else 2/4/8 s) instead of retrying within a second from several connections at once, which kept it rate limited until reads failed and players treated the file as ended.
> - **Airing seasons get their released episodes.** After a season with unreleased episodes is scraped, its released episodes are scraped right away instead of waiting for the daily retry.
> - **Seerr issues for stuck items (optional).** `content.overseerr.issues` opens a Seerr issue when a released movie or episode stays missing, and resolves it once it's available.
> - **Slow sources get replaced (optional).** `stream.slow_replace`: when a file playing in Plex keeps timing out or downloads slower than it plays, Riven tries a fresh link, then stops playback with a message and swaps in another copy at the same path. Several slow files at once, or one still slow after moving to another server, is reported (Apprise) instead of acted on.
> - **Calendar works.** `GET /api/v1/calendar` no longer fails with a 500 (`yield_per` combined with `unique()`).
> - **Real stream errors reported.** A stream whose first connection fails now raises that error (rate limit, dead link...) instead of trio's `task_status.started()` RuntimeError, so the VFS returns the matching errno.
> - **Episodes only take torrents from their season.** Scraping S15E12 no longer accepts S13E12 torrents that then fail to download.
> - **Plex doesn't re-check every folder.** VFS folders keep their modified time until a file is added or removed, instead of always looking just changed.
> - **File times are correct.** VFS file times are read as UTC, so they're no longer hours in the future.
> - **Manual retry and scrape always run.** Retry, Retry Library and auto-scrape replace events queued for later (such as a downloader cooldown) instead of being ignored.
> - **Missing items return 404.** `GET /api/v1/items/<id>` answers 404 for an unknown item instead of 500.
> - **Service names spelled right in settings.** Real-Debrid, Debrid-Link, AllDebrid, MediaFusion, RARBG.
> - **Live progress shows the right service.** The `event_update` stream and `/api/v1/events` list each job under the service running it, so the frontend can show what Riven is doing with an item.
>
> For full details of each change, see the [commits list](https://github.com/coledeb/riven/commits/torbox).

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
> - **Riven Frontend:** Set `repo_owner` to `coledeb`, `branch_enabled` to `true`, and `branch` to `settings-ui`. This uses my [Riven Frontend fork](https://github.com/coledeb/riven-frontend/tree/settings-ui) (current upstream Frontend plus a reworked settings page), which is the Frontend this fork is built and tested with.
>   - Updating the Frontend **WILL** break the embedded UI in DUMB, as DUMB currently expects an older pinned version. See the [instructions](https://github.com/coledeb/riven/blob/torbox/scripts/DUMB-EMBEDDED-UI.md) in this repo's [scripts](https://github.com/coledeb/riven/tree/torbox/scripts) folder for more information on this, as well as a fix.
> - **Riven Backend:** Set `repo_owner` to `coledeb`, `repo_name` to `riven`, `branch_enabled` to `true`, and `branch` to `torbox`.
> - I also recommend setting `auto_update`  to `true`, and `auto_update_mode` to at least `check_only`; I am actively pushing updates as I work on the fork.

I will possibly consider issues or pull requests on this fork, but I offer no guarantee of this.

## License

This project is licensed under the GNU GPLv3 License - see the [LICENSE](LICENSE) file for details
