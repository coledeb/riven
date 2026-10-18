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
> This fork (`coledeb/riven`, branch [`torbox`](https://github.com/coledeb/riven/tree/torbox)) adds [TorBox](https://torbox.app) as a downloader for current Riven. Upstream Riven no longer supports TorBox. The downloader is based on the earlier [TorBox-App/riven](https://github.com/TorBox-App/riven) fork and updated for the current downloader interface and RivenVFS.
>
> It was written with TorBox's rate limits in mind, aiming to be a respectful API client that stays well inside them rather than leaning on retries:
>
> - **Cached-only adds.** Each hash is checked with `checkcached` first, then added with `add_only_if_cached`. Nothing is downloaded from peers, and the hourly add limit isn't spent on uncached torrents.
> - **Paced requests.** General API calls are paced at 240/min, below the documented 300/min, leaving room for other tools sharing the key. Download-link requests (`requestdl`) have their own, much smaller budget and are paced at 15/min.
> - **Back off on 429s.** When TorBox returns a 429, the affected requests are paused for its `Retry-After` period instead of being retried in a loop.
> - **Few link requests.** Links are resolved only when a file is actually opened, then reused.
> - **Fresh account listings.** `mylist` is read with `bypass_cache=true` and paged.
> - **Shared accounts are safe.** Riven only deletes torrents it created itself, never ones that were already on the account.
> - **Offline CDN servers.** TorBox sometimes hands out links on CDN servers it has taken offline (their names resolve to `0.0.0.0`). Such a link is reported as unavailable, so Riven picks another source for that file instead of failing every read.
> - **Expired CDN links.** TorBox's CDN answers an expired link with `400 Bad Request` rather than 403/410. Riven now treats a 400 like a 404: it fetches a fresh link once and retries, instead of failing every read of a file that has been sitting for a while.
>
> The TorBox limits and API behaviour this work relies on come from the [Debrid Media Manager notes on TorBox limits](https://notes.debridmediamanager.com/providers/torbox-limits/), written alongside [zurg](https://github.com/debridmediamanager/zurg-public). Many thanks to the DMM/zurg project for documenting them.
>
> **Other improvements**
>
> Changes in this fork that aren't strictly part of TorBox support:
>
> - **Debridio scraper (TorBox only).** New scraper that gets real torrent names and file lists for Debridio results from TorBox's `checkcached`.
> - **Torrentio language flags kept.** Torrentio's flag emojis are added to titles as words, so language filters catch more dubs.
> - **Plex on another machine (optional).** `updaters.plex.path_mappings` maps Riven's library paths to the paths Plex uses.
> - **Settings changes no longer break the mount.** Saving settings keeps the running RivenVFS mount instead of mounting a second one over it.
> - **Blacklisting a pack for one episode sticks.** An episode playing from a pack now records the pack's blacklist and won't pick it again.
> - **No library flicker on settings changes.** The VFS tree is only rebuilt when a library profile actually changes.
> - **Anime audio preference (optional).** `scraping.anime_audio_order` ranks anime by audio type (dual / japanese / dubbed) in your chosen order.
> - **Keys kept out of logs.** Account keys in logged URLs and errors are replaced with `REDACTED`.
> - **Manual scrape works for shows.** `GET /api/v1/scrape?item_id=<show>` no longer fails with `DetachedInstanceError`.
> - **Re-sourced files don't go missing in Plex.** When a link dies, the file stays in place and is swapped to the new source, and reads wait for the swap.
> - **Airing seasons get their released episodes.** After a season with unreleased episodes is scraped, its released episodes are scraped right away instead of waiting for the daily retry.
>
> For full details of each change, see the [commits on the `torbox` branch](https://github.com/coledeb/riven/commits/torbox).

Services currently supported:

| Type              | Supported                                                                         |
| ----------------- | --------------------------------------------------------------------------------- |
| Debrid services   | Real Debrid, All Debrid, Debrid-Link, TorBox                                      |
| Content services  | Plex Watchlist, Overseerr, Mdblist, Listrr, Trakt                                 |
| Scraping services | AIOStreams, Comet, Debridio, Jackett, Torrentio, Orionoid, Mediafusion, Prowlarr, Zilean, Rarbg |
| Media servers     | Plex, Jellyfin, Emby                                                              |

## Documentation

For installation, Plex setup, RivenVFS and caching, library profiles, naming templates and contributing, see the [upstream Riven README](https://github.com/rivenmedia/riven#readme). Everything there applies to this fork too; the fork only adds the TorBox downloader and the changes listed above.

## License

This project is licensed under the GNU GPLv3 License - see the [LICENSE](LICENSE) file for details
