# Missing source filenames in Plex playback sessions

## Incident and evidence (2026-10-19, America/Chicago)

Lanterns S01E08 was playing through Plex Web on a remote user's connection.
Riven logged 122 `ChunksTooSlowException` messages between 16:44 and 16:57,
each for reads exceeding the configured 30-second chunk wait. Concurrent
reads produced duplicate messages: this is not a count of viewer buffering events.
The session was paused at 05:23 when inspected around 16:58.

The live settings had `stream.slow_replace.enabled=true`, two stalls within
300 seconds, a 15-second scheduled check, and a six-hour replacement cooldown.
There were no `Slow streams:` action messages. The protection was installed
and enabled; the session matching stage silently discarded the affected file.

Read-only checks against both raw Plex JSON and the installed PlexAPI confirmed:

- `/status/sessions`: ratingKey `798`, media/part ID `1014`, **Part.file absent**.
- Video was copied (HEVC); audio was transcoded (EAC3 to AAC).
- PlexAPI exposed the session part's `file` as `None`.
- `PlexServer.fetchItem('798')` returned part ID `1014` with the correct
  `\\wsl-media\riven\shows\Lanterns (2026) {tvdb-376098}\Season 01\Lanterns - s01e08.mkv` path.

The source reads were stalling before Plex delivered media to the client. This
investigation did not isolate CDN, network, or Riven cache/streaming causes.
An expired CDN link was refreshed at 16:43; that is not proof of the later cause.

## Root cause and affected callers

`SlowStreamMonitor._find_slow` calls `PlexUpdater.sessions_playing` before
assessing recorded stalls. The shared `playing_sessions` helper previously
compared only `session.media[*].parts[*].file` to mapped Riven paths. With
`file=None`, it returned no sessions, so neither link refresh nor replacement
was attempted despite the recorded timeouts.

TorBox account import uses the same updater method to find playback before
replacing existing files. The fix therefore also restores its ability to find
and stop these transcoded sessions. A session's paused state was not the cause:
the matcher does not filter by playing/paused state.

## Fix

`PlexUpdater.sessions_playing` now supplies the server's `fetchItem` callback.
For session parts missing a filename, the helper fetches library metadata by
ratingKey and resolves the source by **part ID** (normalizing integer/string IDs).
It then applies the existing path mapping and Windows case-insensitive matching.

- Direct-play sessions with filenames require no extra metadata lookup.
- If the session has no part ID, fallback is allowed only when the library item
  has exactly one file-bearing part. Ambiguous versions are not guessed.
- An explicit part ID that no longer exists is not matched to another version.
- Results remain the original live session objects, preserving `stop()` and
  session identity; library items must never be returned in their place.
- Metadata is cached per matching call, not across calls, to avoid stale source
  paths after replacements. Failed lookups are also cached for that call.
- Lookup failure logs the item ID and exception type, without exception text
  that might expose tokens/URLs, and does not hide other sessions.

No changes to thresholds, settings, replacement policy, blacklists, or downloader
selection were required.

## Validation

Run in the existing development container:

```sh
docker exec riven-dev sh -c 'cd /app && /venv/bin/python -m pytest -q -p no:cacheprovider src/tests/test_plex_playback.py src/tests/test_slow_streams.py src/tests/test_torbox_import.py'
```

Result: **40 passed**. Ten new cases cover missing filenames through the real
updater, retained stoppable session identity, direct-play behavior, alternate
versions, missing/stale IDs, absent metadata, lookup failure isolation, cache
freshness, and the monitor integration path. The integration test records two
stalls, verifies a fresh-link attempt, records further stalls, and verifies that
the replacement handler receives the original Plex session. Replacement side
effects are mocked: this does not claim a real playback stop or source swap was
performed during testing. Existing cooldown and outage-guard tests also pass.

## Deployment and follow-up for Claude

The change is committed locally; see HANDOFF.md for the commit and any subsequent
deployment status. It is not live until pushed to `coledeb/riven` branch `main`
and the Riven Backend is restarted through DUMB. DUMB downloads that branch on
every restart; copying files into the running backend is not durable deployment.
Follow the user's existing restart rules. No migration or settings changes needed.

After deployment, a file with missing session filenames should be recognized.
Repeated stalls should log a fresh-link attempt, then continued stalls on the
same host should stop playback, blacklist that item's slow source, and re-source
at the same VFS path. If no different link is available, replacement can happen
on the first check. Confirm the actual stop, blacklist reason, and new source in
a real incident; the unit tests do not establish production success.

Existing intentional exceptions still apply: multiple different torrents slow
at once, or continued slowness after moving to a different CDN host, report a
connection/debrid issue without replacement. The six-hour per-item cooldown and
stop-failure safeguards also remain. This fix removes the missing-filename blind
spot; it does not override those policies or guarantee an alternative is available.
