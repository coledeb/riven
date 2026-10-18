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
> - **Debridio scraper (TorBox only).** Debridio's Stremio addon returns playback links whose file-name titles hide season packs. The scraper takes each hash from the link, then asks TorBox's free `checkcached` (up to 100 hashes per call, paced at 60/min) for the real torrent name and file list. Uncached results are dropped. If TorBox is throttled, it falls back to Debridio's titles without waiting. Debridio's language flags are appended as words (e.g. `[MULTI FRENCH]`) so Riven's language filters still apply. A rejected key or inactive subscription is logged as such.
> - **Torrentio language flags kept.** Torrentio's flag line (`🇬🇧 / 🇷🇺`) is appended to titles as words, so language exclusions also catch dubs whose names don't say the language (e.g. Cyrillic-only names).
> - **Plex on another machine (optional).** `updaters.plex.path_mappings` maps Riven's library paths to the paths Plex uses, e.g. `{"/mnt/debrid/riven": "\\\\server\\riven"}`. Windows targets get backslashes and case-insensitive matching. Leave it empty when Plex sees the same paths as Riven.
> - **Settings changes no longer break the mount.** Saving settings used to build a second RivenVFS and mount it over the running one, which logged "RivenVFS not mounted" or crashed the backend once the library had content. The running mount is now kept and given the new downloader; it's only remounted if `mount_path` changes or the mount has died. Startup also waits for the mount before checking it.
> - **Blacklisting a pack for one episode sticks.** When an episode played from a season or show pack, blacklisting its stream did nothing, because the pack wasn't in the episode's own stream list. The episode then picked the same broken file again. Now the blacklist is recorded on the episode, and pack downloads skip episodes that have blacklisted that pack.
> - **No library flicker on settings changes.** Every save used to clear the whole VFS tree and rebuild it, because the "library profiles unchanged" check crashed on list-valued filter rules (the default `example_kids` profile has them). While it rebuilt, folders were briefly missing, and a media server scanning then could drop items. The tree is now only rebuilt when a profile actually changes, and readers wait for the rebuild to finish instead of seeing a half-built tree.
> - **Anime audio preference (optional).** `scraping.anime_audio_order` ranks anime by audio in the order you choose, best first, e.g. `["dual", "japanese", "dubbed"]` (`dual` = Japanese + English). Unlisted types rank lowest; an empty list (the default) turns it off. It applies within each resolution. Untagged releases and "ESub"/"English Subbed" count as Japanese audio, since anime release names rarely state their audio.
> - **Keys kept out of logs.** Debrid CDN links can carry the account key in the URL (TorBox: `?token=<api key>`), and failed reads logged the full URL. Logged messages and errors now show `token=REDACTED` (also `apikey`, `key`, `password`), and log files no longer print local variables in tracebacks.
> - **Manual scrape works for shows.** `GET /api/v1/scrape?item_id=<show>` failed for every season with `DetachedInstanceError`; parents are now linked first, as the streaming variant already did.
> - **Re-sourced files don't go missing in Plex.** When a link died, the file vanished from the mount until a new source was downloaded. A media server scanning in that gap marked the item unavailable. Even after the scan, it kept the old file's details, because the new file has the same name, so playback could hang. The file now stays in place and is switched to the new source when it's ready. Opening or reading it in the meantime waits up to 30 s, so playback that hit the dead link carries on with the new source instead of failing. If no new source arrives within 10 minutes, the file is removed (and added back as usual if a source turns up later). Media servers are asked to rescan again 15 s later, after SMB clients' file-info cache has expired, so they pick up the new size and format.

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
