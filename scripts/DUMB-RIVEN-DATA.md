# Riven's data folder in DUMB

If you run Riven in [DUMB](https://github.com/I-am-PUID-0/DUMB) on a branch (`branch_enabled=true`), Riven Backend can come back after a restart with **all settings reset to defaults**: no downloader, no scrapers, no updater, and every stream fails. Your real settings aren't gone; Riven just isn't looking at them.

## Why

1. On container start, DUMB moves `/riven/backend/data` (settings, state files, VFS cache) to `/data/riven` (`data_root`, normally your bind-mounted `data` folder) and puts a symlink in its place. If the folder was still inside the container, this happens on the first container restart after you set Riven up.
2. When Riven Backend is reinstalled (a new commit on the branch, with `clear_on_update` on, the default), DUMB clears `/riven/backend` except the paths in `exclude_dirs`, which includes `/riven/backend/data`. But `DeferredClearTransaction` in `utils/transactional_install.py` resolves each excluded path first. The symlink resolves to `/data/riven`, outside `/riven/backend`, so the exclusion is dropped and the symlink is removed with the old install.
3. Riven then starts with no `data` folder, creates a new one, and writes default settings to it.

So after the first restart, **every Riven Backend reinstall** breaks it again. `RuntimeRollbackSnapshot` in the same file has the same code. Seen with DUMB 2.23.0.

## What the script does

**`patch-dumb-riven-data.sh`** does two things:

- **Patch:** in `utils/transactional_install.py`, both places first try the path as given and only then resolve it, like `utils/download.py` already does. The symlink then stays excluded. The original is kept as `transactional_install.py.orig`.
  - DUMB loads this file when it starts, so the patch takes effect at the **next DUMB container restart**.
  - If the patch is already in place it does nothing. If DUMB has changed that code (for example after an update that fixes it), it logs a warning and changes nothing.
- **Repair:** if `/riven/backend/data` isn't the symlink to `/data/riven` but `/data/riven/settings.json` exists, it stops Riven Backend through DUMB's API, moves the stray folder to `/riven/backend/data.stray-<time>`, puts the symlink back and starts Riven Backend. It uses a 5-minute token that DUMB's own code creates inside the container; no password is needed and the token is never printed. If the link is fine, it does nothing.

The patch lives in the container's writable layer, so it is lost when the container is recreated (for example by a DUMB image update). On that first start DUMB reinstalls Riven with the unpatched code; the script then patches DUMB and repairs the link, so Riven runs on default settings for a few minutes at most.

## Install

Uses the same systemd user service as the [embedded UI fix](DUMB-EMBEDDED-UI.md): `dumb-embed-watch.service` runs every installed `patch-dumb-*.sh` script once when it starts and again whenever the DUMB container starts. Same requirements as there (systemd, `docker` without sudo, container named `DUMB` or `DUMB_CONTAINER` set), and see there for keeping the service running after a reboot.

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
cp scripts/patch-dumb-riven-data.sh ~/.local/bin/
chmod +x ~/.local/bin/patch-dumb-riven-data.sh
cp scripts/dumb-embed-watch.service ~/.config/systemd/user/

# One-off run: patches now and repairs the link if needed
~/.local/bin/patch-dumb-riven-data.sh

systemctl --user daemon-reload
systemctl --user enable --now dumb-embed-watch.service
systemctl --user restart dumb-embed-watch.service   # if it was already running
```

Then restart the DUMB container (a restart, not a recreate) so DUMB loads the patch.

Check: `docker exec DUMB ls -la /riven/backend` should show `data -> /data/riven`. Logs: `journalctl --user -u dumb-embed-watch`.

## Undo

```bash
rm ~/.local/bin/patch-dumb-riven-data.sh   # the service skips scripts that aren't installed
docker exec DUMB cp /utils/transactional_install.py.orig /utils/transactional_install.py
```

Then restart the DUMB container.
