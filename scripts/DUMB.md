# Running this fork in DUMB

[DUMB](https://github.com/I-am-PUID-0/DUMB) runs Riven fine on a branch (`branch_enabled=true`, `branch=main`), but two DUMB issues need a fix. Each has a script in this folder, and one systemd user service runs them both:

| Problem | Script |
| --- | --- |
| [Riven Frontend's Embedded UI tab](#1-riven-frontend-in-dumbs-embedded-ui) is unstyled and login fails | `patch-dumb-embed.sh` |
| [Riven Backend comes back on default settings](#2-riven-backend-loses-its-settings) after a restart | `patch-dumb-riven-data.sh` |
| Runs both on every DUMB container start | `dumb-embed-watch.service` |

Both scripts patch files inside the DUMB container (as root, which `docker exec` does by default), keep the original next to it as `.orig`, and do nothing if the patch is already in place. If DUMB has changed the code they patch (for example after an update that fixes it), they log a warning and change nothing. Neither needs a password: where they call DUMB's API, they use a short-lived token that DUMB's own code creates inside the container, and the token is never printed.

The patches live in the container's writable layer, so a recreate (for example a DUMB image update) loses them. That's why the service reruns the scripts every time the DUMB container starts.

## Install

Requirements: Linux (or WSL2) with systemd, your user can run `docker` without sudo, and the container is named `DUMB`. If it has another name, set `DUMB_CONTAINER` in the service file and change `container=DUMB` there too.

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
cp scripts/patch-dumb-embed.sh scripts/patch-dumb-riven-data.sh ~/.local/bin/
chmod +x ~/.local/bin/patch-dumb-*.sh
cp scripts/dumb-embed-watch.service ~/.config/systemd/user/

# One-off runs
~/.local/bin/patch-dumb-embed.sh        # patches now and restarts DUMB Frontend
~/.local/bin/patch-dumb-riven-data.sh   # patches now, repairs the data link if needed

systemctl --user daemon-reload
systemctl --user enable --now dumb-embed-watch.service
systemctl --user restart dumb-embed-watch.service   # if it was already running
```

Then restart the DUMB container (a restart, not a recreate) so DUMB loads the data-folder patch.

You can install just one of the scripts: the service skips any that aren't in `~/.local/bin`.

Check:
- Reload the Riven Frontend **Embedded UI** tab in DUMB. It should show the styled Riven login page.
- `docker exec DUMB ls -la /riven/backend` should show `data -> /data/riven`.
- Logs: `journalctl --user -u dumb-embed-watch`.

### Keeping the service running after a reboot

A systemd *user* service only runs while your user has a login session. Pick one of these:

- **Enable linger (recommended).** Your user services then start at boot and keep running with no session open. Run once:
  ```bash
  sudo loginctl enable-linger "$USER"
  ```
- **WSL only: start a WSL session when you log in to Windows.** Starting `wsl.exe` opens a session for your user, and that starts your user services. For example, put a shortcut to `wsl.exe` in your Startup folder (`Win+R` → `shell:startup`).
  - This starts at Windows *logon*, not boot.
  - The services keep running only while that session stays open. Closing the WSL window ends it.
  - WSL may also shut down when idle. Setting `instanceIdleTimeout=-1` under `[general]` in `%UserProfile%\.wslconfig` prevents that.

  Linger has none of these limits.

## 1. Riven Frontend in DUMB's Embedded UI

**Symptom:** with a current Riven Frontend, its **Embedded UI** tab in DUMB shows an unstyled page (plain HTML, giant background image), and logging in or registering there fails.

**Why.** DUMB's UI proxy has two Riven Frontend special cases, written for the old frontend it pins (v0.17.0):

1. It injects `<base href="/ui/riven_frontend/">` into the page. The current frontend links its CSS and JS as `../_app/...`, which the base tag turns into `/ui/_app/...` (404).
2. It sends `/ui/riven_frontend/api/*` to Riven Backend. The current frontend serves its own `/api` (login, passkeys), so those calls fail.
3. Not a special case, but related: the frontend's `ORIGIN` is `http://localhost:3000`. If you open DUMB on any other address (LAN IP, reverse proxy, Cloudflare tunnel), SvelteKit rejects the login and register forms with 403 "Cross-site POST form submissions are forbidden", and the button appears to do nothing.
4. Also not a special case: DUMB opens the embed at `/ui/riven_frontend`, without a trailing slash. Once you're logged in, the home page links its CSS and JS as `./_app/...`, which then also resolves to `/ui/_app/...` (404), so the page is unstyled again.

All of these are handled in `/dumb/frontend/.output/server/chunks/nitro/nitro.mjs` inside the DUMB container. Tested with DUMB 1.89.0 and Riven Frontend v1.0.0-beta.3.

**What `patch-dumb-embed.sh` does:** turns both special cases off and, for (3), has DUMB's proxy rewrite `Origin` to `http://localhost:3000` on Riven Frontend requests, but only when it matches the address you're browsing, so forms posted from other sites are still rejected. For (4), it redirects exactly `/ui/riven_frontend` to `/ui/riven_frontend/`. Then it restarts "DUMB Frontend" through DUMB's API (2-minute token). That only reloads DUMB's dashboard; it doesn't touch Riven or your other services.

Once DUMB itself supports the current frontend, the script will report that the pattern isn't found and do nothing.

## 2. Riven Backend loses its settings

**Symptom:** after a DUMB container restart (a reboot is enough), Riven Backend comes back with **all settings reset to defaults**: no downloader, no scrapers, no updater, and every stream fails. Your real settings aren't gone; Riven just isn't looking at them.

**Why.**

1. On container start, DUMB moves `/riven/backend/data` (settings, state files, VFS cache) to `/data/riven` (`data_root`, normally your bind-mounted `data` folder) and puts a symlink in its place. If the folder was still inside the container, this happens on the first container restart after you set Riven up.
2. When Riven Backend is reinstalled (a new commit on the branch, with `clear_on_update` on, the default), DUMB clears `/riven/backend` except the paths in `exclude_dirs`, which includes `/riven/backend/data`. But `DeferredClearTransaction` in `utils/transactional_install.py` resolves each excluded path first. The symlink resolves to `/data/riven`, outside `/riven/backend`, so the exclusion is dropped and the symlink is removed with the old install.
3. Riven then starts with no `data` folder, creates a new one, and writes default settings to it.

So after the first restart, **every Riven Backend reinstall** breaks it again. `RuntimeRollbackSnapshot` in the same file has the same code. Seen with DUMB 2.23.0.

**What `patch-dumb-riven-data.sh` does:**

- **Patch:** in `utils/transactional_install.py`, both places first try the path as given and only then resolve it, like `utils/download.py` already does. The symlink then stays excluded. DUMB loads this file when it starts, so the patch takes effect at the **next DUMB container restart**.
- **Repair:** if `/riven/backend/data` isn't the symlink to `/data/riven` but `/data/riven/settings.json` exists, it stops Riven Backend through DUMB's API (5-minute token), moves the stray folder to `/riven/backend/data.stray-<time>`, puts the symlink back and starts Riven Backend. If the link is fine, it does nothing.

After a recreate, DUMB's first start reinstalls Riven with the unpatched code; the script then patches DUMB and repairs the link, so Riven runs on default settings for a few minutes at most.

## Undo

```bash
systemctl --user disable --now dumb-embed-watch.service
rm ~/.config/systemd/user/dumb-embed-watch.service ~/.local/bin/patch-dumb-*.sh
docker exec DUMB cp /dumb/frontend/.output/server/chunks/nitro/nitro.mjs.orig \
  /dumb/frontend/.output/server/chunks/nitro/nitro.mjs
docker exec DUMB cp /utils/transactional_install.py.orig /utils/transactional_install.py
```

Then restart the DUMB container. To undo just one fix, remove only its script from `~/.local/bin` (the service skips it) and restore only its `.orig` file.
