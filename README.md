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
> - **Cached-only adds.** Hashes are checked with `checkcached` and added with `add_only_if_cached`, so nothing downloads from peers or wastes the hourly add limit.
> - **Paced requests.** API calls are paced at 240/min (limit 300) and download-link requests at 15/min, leaving room for other tools on the key.
> - **Back off on 429s.** Rate-limited requests wait out TorBox's `Retry-After` instead of retrying in a loop.
> - **Few link requests.** Links are resolved only when a file is opened, then reused.
> - **Fresh account listings.** `mylist` is read with `bypass_cache=true` and paged.
> - **Shared accounts are safe.** Riven only deletes torrents it created (`only_manage_own_torrents`) and never ones its library still uses.
> - **No leftover torrents.** Torrents Riven added that no file uses any more (replaced, blacklisted or removed) are deleted after 30 minutes unused (`remove_unused_torrents`, `unused_grace_minutes`). Riven remembers which torrents it added across restarts.
> - **Import from the TorBox account (optional).** `account_import` links torrents you add elsewhere (e.g. [Debrid Media Manager](https://debridmediamanager.com)), matched by name including two-language names, to missing movies and episodes. With `replace_existing` they also replace existing files in place (stopping Plex playback first with `stop_playback_to_replace`), and unmatched ones are reported via Apprise.
> - **Offline CDN servers.** Links on servers TorBox has taken offline (resolving to `0.0.0.0`) are treated as unavailable, so Riven picks another source.
> - **Expired CDN links.** A `400 Bad Request` from the CDN is treated like a 404: Riven fetches a fresh link once and retries.
>
> The TorBox limits and API behaviour this work relies on come from the [Debrid Media Manager notes on TorBox limits](https://notes.debridmediamanager.com/providers/torbox-limits/), written alongside [zurg](https://github.com/debridmediamanager/zurg-public). Many thanks to the DMM/zurg project for documenting them.
>
> **Other improvements**
>
> Changes in this fork that aren't strictly part of TorBox support:
>
> - **Debridio scraper (TorBox only).** Gets real torrent names and file lists for Debridio results from TorBox, with uploader tags stripped and Torrentio's title kept for the same torrent.
> - **Torrentio language flags kept.** Flag emojis become words in titles, so language filters catch more dubs.
> - **Plex on another machine (optional).** `updaters.plex.path_mappings` maps Riven's paths to Plex's.
> - **Settings changes no longer break the mount.** Saving settings keeps the running RivenVFS mount instead of mounting a second one.
> - **Blacklisting a pack for one episode sticks.** The pack is blacklisted and not picked again.
> - **No library flicker on settings changes.** The VFS tree is rebuilt only when a library profile changes.
> - **Anime audio preference (optional).** `scraping.anime_audio_order` ranks anime by dual / japanese / dubbed audio.
> - **Keys kept out of logs.** Account keys in logged URLs and errors are replaced with `REDACTED`.
> - **Manual scrape works for shows.** No more `DetachedInstanceError` from `GET /api/v1/scrape`.
> - **Re-sourced files don't go missing in Plex.** A file with a dead link stays in place and switches to the new source.
> - **Playback survives a rate-limited CDN.** A `429` pauses every connection to that server for its `Retry-After` (else 2/4/8 s), instead of retrying until players give up.
> - **Airing seasons get their released episodes.** Released episodes are scraped right after their season, not at the daily retry.
> - **Seerr season requests respected.** A request for seasons 11-13 fetches only those; the other seasons are paused, and a later request for more seasons resumes them. `POST /api/v1/items/pause` with `"remove_files": true` also pauses completed items and removes their files without blacklisting.
> - **Seerr issues for stuck items (optional).** `content.overseerr.issues` opens a Seerr issue for a released item that stays missing and resolves it once available.
> - **Slow sources get replaced (optional).** `stream.slow_replace` retries a fresh link for a file that keeps stalling in Plex, then swaps in another copy at the same path. Widespread slowness is reported via Apprise instead.
> - **Blacklists expire for temporary problems.** Each blacklisted torrent records why: a pulled CDN server, a slow source or a torrent removed from the account comes back after 24 h, not cached or other download errors after 72 h, and wrong files or manual blacklists never come back. A repeat doubles the time, and after `max_strikes` it's permanent (`scraping.blacklist`). Works with every debrid service; only the pulled-server check is TorBox-specific.
> - **Failed downloads are remembered.** When every candidate torrent fails, their blacklist entries are now saved and the item moves on to re-scraping, instead of retrying the same torrents.
> - **Calendar works.** `GET /api/v1/calendar` no longer returns 500.
> - **Real stream errors reported.** A failed first connection raises its real error, so the VFS returns the matching errno.
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
>   - Updating the Frontend **WILL** break the embedded UI in DUMB, as DUMB currently expects an older pinned version. See the [instructions](https://github.com/coledeb/riven/blob/main/scripts/DUMB-EMBEDDED-UI.md) in this repo's [scripts](https://github.com/coledeb/riven/tree/main/scripts) folder for more information on this, as well as a fix.
> - **Riven Backend:** Set `repo_owner` to `coledeb`, `repo_name` to `riven`, `branch_enabled` to `true`, and `branch` to `main`.
> - I also recommend setting `auto_update`  to `true`, and `auto_update_mode` to at least `check_only`; I am actively pushing updates as I work on the fork.

I will possibly consider issues or pull requests on this fork, but I offer no guarantee of this.

## License

This project is licensed under the GNU GPLv3 License - see the [LICENSE](LICENSE) file for details
