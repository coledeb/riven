#!/usr/bin/env bash
# Fix Riven Frontend styling in DUMB's Embedded UI.
#
# DUMB's UI proxy injects <base href="/ui/riven_frontend/"> into Riven Frontend
# pages (SVELTEKIT_SPA_SERVICES in nitro.mjs). The current frontend links its CSS
# as "../_app/...", which the base tag turns into /ui/_app/... (404), so the page
# is unstyled. DUMB also sends /ui/riven_frontend/api/* to Riven Backend, but the
# current frontend serves its own /api (login, passkeys), so login fails.
# Both were written for DUMB's pinned frontend v0.17. This turns both off, then
# restarts "DUMB Frontend" through DUMB's API with a 2-minute token DUMB mints itself.
#
# Idempotent: does nothing if already patched. The patch lives in the container
# layer, so it is lost when the container is recreated (DUMB image update);
# dumb-embed-watch.service reruns this script whenever the DUMB container starts.
# See DUMB-EMBEDDED-UI.md next to this file.
#
# Usage: patch-dumb-embed.sh [--wait]   (--wait: wait up to 15 min for DUMB to be up)
#        DUMB_CONTAINER=<name> if your container isn't called "DUMB".
# Undo:  docker exec DUMB cp /dumb/frontend/.output/server/chunks/nitro/nitro.mjs.orig \
#          /dumb/frontend/.output/server/chunks/nitro/nitro.mjs   (then restart DUMB Frontend)
set -u
C="${DUMB_CONTAINER:-DUMB}"
F=/dumb/frontend/.output/server/chunks/nitro/nitro.mjs
# Pairs of "old<TAB>new", one per line.
PATCHES='SVELTEKIT_SPA_SERVICES = /* @__PURE__ */ new Set(["riven_frontend"])	SVELTEKIT_SPA_SERVICES = /* @__PURE__ */ new Set([])
if (/^\/ui\/riven_frontend\/api(?:[/?]|$)/.test(reqUrl)) {	if (false && /^\/ui\/riven_frontend\/api(?:[/?]|$)/.test(reqUrl)) {'
log() { echo "$(date '+%F %T') patch-dumb-embed: $*"; }

if [ "${1:-}" = "--wait" ]; then
  # After a recreate DUMB installs/starts services first; wait for its API.
  for _ in $(seq 1 90); do
    docker exec "$C" curl -sf -o /dev/null http://127.0.0.1:8000/health 2>/dev/null && break
    sleep 10
  done
  sleep 30  # let DUMB finish starting its own frontend before we restart it
fi

docker exec -i -e PATCHES="$PATCHES" -e F="$F" "$C" /venv/bin/python - <<'PY'
import os, shutil, sys
f = os.environ["F"]
pairs = [l.split("\t") for l in os.environ["PATCHES"].splitlines() if l]
s = open(f).read()
todo = [(o, n) for o, n in pairs if n not in s]
if not todo:
    sys.exit(3)  # already patched
missing = [o for o, n in todo if s.count(o) != 1]
if missing:
    print("pattern not found:", *missing, sep="\n  ")
    sys.exit(2)
if not os.path.exists(f + ".orig"):
    shutil.copy2(f, f + ".orig")
for o, n in todo:
    s = s.replace(o, n)
open(f, "w").write(s)
PY
rc=$?
case $rc in
  0) ;;
  3) log "already patched"; exit 0 ;;
  2) log "WARNING: DUMB changed this code; nothing done"; exit 1 ;;
  *) log "patch failed ($rc)"; exit 1 ;;
esac
log "patched $F (original kept as nitro.mjs.orig)"

# Restart DUMB Frontend via DUMB's API. Token is created and used inside the
# container only, never printed.
docker exec -i "$C" sh -c 'cd / && /venv/bin/python -' <<'PY'
import sys, json, urllib.request
from datetime import timedelta
sys.path.insert(0, "/")
from utils.auth_config import AuthConfigManager
from utils.auth import create_access_token
headers = {"Content-Type": "application/json"}
a = AuthConfigManager()
if a.is_auth_enabled():
    user = next(u["username"] for u in a.get_all_users() if not u.get("disabled"))
    headers["Authorization"] = "Bearer " + create_access_token(user, timedelta(minutes=2))
req = urllib.request.Request(
    "http://127.0.0.1:8000/process/restart-service",
    data=json.dumps({"process_name": "DUMB Frontend"}).encode(), headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.loads(r.read() or b"{}")
        print("restart DUMB Frontend:", r.status, body.get("status", ""))
except Exception as e:
    print("restart DUMB Frontend FAILED:", getattr(e, "code", ""), str(e)[:200])
    sys.exit(1)
PY
