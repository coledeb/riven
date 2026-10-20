# Riven Frontend in DUMB's Embedded UI

If you run Riven in [DUMB](https://github.com/I-am-PUID-0/DUMB) with a current Riven Frontend (`branch_enabled=true`, `branch=main`), the frontend's **Embedded UI** tab in DUMB shows an unstyled page (plain HTML, giant background image), and logging in or registering there fails.

## Why

DUMB's UI proxy has two Riven Frontend special cases, written for the old frontend it pins (v0.17.0):

1. It injects `<base href="/ui/riven_frontend/">` into the page. The current frontend links its CSS and JS as `../_app/...`, which the base tag turns into `/ui/_app/...` (404).
2. It sends `/ui/riven_frontend/api/*` to Riven Backend. The current frontend serves its own `/api` (login, passkeys), so those calls fail.
3. Not a special case, but related: the frontend's `ORIGIN` is `http://localhost:3000`. If you open DUMB on any other address (LAN IP, reverse proxy, Cloudflare tunnel), SvelteKit rejects the login and register forms with 403 "Cross-site POST form submissions are forbidden", and the button appears to do nothing.
4. Also not a special case: DUMB opens the embed at `/ui/riven_frontend`, without a trailing slash. Once you're logged in, the home page links its CSS and JS as `./_app/...`, which then also resolves to `/ui/_app/...` (404), so the page is unstyled again.

All of these are handled in `/dumb/frontend/.output/server/chunks/nitro/nitro.mjs` inside the DUMB container. Tested with DUMB 1.89.0 and Riven Frontend v1.0.0-beta.3.

## What the files here do

- **`patch-dumb-embed.sh`** turns both special cases off and, for (3), has DUMB's proxy rewrite `Origin` to `http://localhost:3000` on Riven Frontend requests, but only when it matches the address you're browsing, so forms posted from other sites are still rejected. For (4), it redirects exactly `/ui/riven_frontend` to `/ui/riven_frontend/`. It keeps the original as `nitro.mjs.orig`. Then it restarts "DUMB Frontend" through DUMB's API, using a 2-minute token that DUMB's own code creates inside the container. No password is needed and the token is never printed.
  - If the patch is already in place, it does nothing.
  - If DUMB has changed that code (for example after an update that fixes it), it logs a warning and changes nothing.
- **`dumb-embed-watch.service`** is a systemd user service. It also runs [`patch-dumb-riven-data.sh`](DUMB-RIVEN-DATA.md) if that's installed. The patch lives in the container's writable layer, so it is lost when the container is recreated (for example by a DUMB image update). The service runs the script once when it starts, then again every time the DUMB container starts. Each time, the script waits for DUMB's API before patching.

Restarting DUMB Frontend only reloads DUMB's dashboard. It doesn't touch Riven or your other services.

## Install

Requirements: Linux (or WSL2) with systemd, your user can run `docker` without sudo, and the container is named `DUMB`. If it has another name, edit both places in the service file.

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
cp scripts/patch-dumb-embed.sh ~/.local/bin/
chmod +x ~/.local/bin/patch-dumb-embed.sh
cp scripts/dumb-embed-watch.service ~/.config/systemd/user/

# One-off run: patches now and restarts DUMB Frontend
~/.local/bin/patch-dumb-embed.sh

systemctl --user daemon-reload
systemctl --user enable --now dumb-embed-watch.service
```

Check that it works: reload the Riven Frontend **Embedded UI** tab in DUMB. It should show the styled Riven login page. Logs: `journalctl --user -u dumb-embed-watch`.

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

## Undo

```bash
systemctl --user disable --now dumb-embed-watch.service
rm ~/.config/systemd/user/dumb-embed-watch.service
docker exec DUMB cp /dumb/frontend/.output/server/chunks/nitro/nitro.mjs.orig \
  /dumb/frontend/.output/server/chunks/nitro/nitro.mjs
```

Then restart **DUMB Frontend** in the DUMB dashboard.

## Notes

- Once DUMB itself supports the current frontend, the script will report that the pattern isn't found and do nothing. You can then remove the service.
- The script runs as root inside the container (`docker exec` does this by default) and only changes that one file.
