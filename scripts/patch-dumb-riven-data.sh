#!/usr/bin/env bash
# Keep Riven's data dir (/riven/backend/data -> /data/riven) across DUMB restarts and
# Riven Backend reinstalls.
#
# DUMB moves /riven/backend/data to /data/riven on container start and leaves a
# symlink. When Riven Backend is reinstalled (new branch commit, clear_on_update),
# DeferredClearTransaction (utils/transactional_install.py) normalizes exclude_dirs
# with Path.resolve(): the symlink resolves to /data/riven, outside /riven/backend,
# so the exclusion is dropped and the symlink is cleared with the old install. Riven
# then starts with a fresh data dir on DEFAULT settings.
# RuntimeRollbackSnapshot has the same code.
#
# 1. Patch: try the unresolved absolute path first (as utils/download.py does), so
#    the symlink itself stays excluded. DUMB imports this module at startup, so the
#    patch takes effect at the next DUMB container (re)start. Lost on a recreate;
#    dumb-embed-watch.service reruns this script on every DUMB container start.
# 2. Heal: once DUMB has finished starting, if /riven/backend/data isn't the symlink
#    but /data/riven has Riven's settings, stop Riven Backend, move the stray dir aside
#    (/riven/backend/data.stray-<time>), relink, start Riven Backend.
#
# See DUMB.md next to this file.
#
# Usage: patch-dumb-riven-data.sh [--wait]   (--wait: wait up to 15 min for DUMB)
#        DUMB_CONTAINER=<name> if your container isn't called "DUMB".
# Undo patch: docker exec DUMB cp /utils/transactional_install.py.orig /utils/transactional_install.py
set -u
C="${DUMB_CONTAINER:-DUMB}"
log() { echo "$(date '+%F %T') patch-dumb-riven-data: $*"; }

if [ "${1:-}" = "--wait" ]; then
  for _ in $(seq 1 90); do
    docker exec "$C" curl -sf -o /dev/null http://127.0.0.1:8000/health 2>/dev/null && break
    sleep 10
  done
  WAIT=1  # the repair below also waits for DUMB's startup to finish
fi

docker exec -i "$C" /venv/bin/python - <<'PY'
import os, shutil, sys
f = "/utils/transactional_install.py"
old = """                try:
                    relative = candidate.resolve().relative_to(self.target)
                except ValueError:
                    continue
"""
new = """                try:
                    relative = Path(os.path.abspath(candidate)).relative_to(self.target)
                except ValueError:
                    try:
                        relative = candidate.resolve().relative_to(self.target)
                    except ValueError:
                        continue
"""
s = open(f).read()
if "Path(os.path.abspath(candidate)).relative_to(self.target)" in s:
    sys.exit(3)
if s.count(old) != 2:
    print(f"expected 2 matches, found {s.count(old)}")
    sys.exit(2)
if not os.path.exists(f + ".orig"):
    shutil.copy2(f, f + ".orig")
open(f, "w").write(s.replace(old, new))
PY
case $? in
  0) log "patched transactional_install.py (live after the next DUMB container restart)" ;;
  3) log "transactional_install.py already patched" ;;
  2) log "WARNING: DUMB changed transactional_install.py; not patched" ;;
  *) log "patch failed" ;;
esac

# Heal a lost symlink. Token is minted and used inside the container, never printed.
# Never while DUMB is still starting: its own Riven Backend install would clear the
# new link again (seen on a recreate, log 133). With --wait, wait for startup to end.
docker exec -i -e WAIT="${WAIT:-0}" "$C" sh -c 'cd / && /venv/bin/python -' <<'PY'
import os, sys, json, time, urllib.request
from datetime import timedelta
link, real = "/riven/backend/data", "/data/riven"
sys.path.insert(0, "/")
from utils.auth_config import AuthConfigManager
from utils.auth import create_access_token
a = AuthConfigManager()
def headers():
    h = {"Content-Type": "application/json"}
    if a.is_auth_enabled():
        user = next(u["username"] for u in a.get_all_users() if not u.get("disabled"))
        h["Authorization"] = "Bearer " + create_access_token(user, timedelta(minutes=5))
    return h
def api(method, path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8000" + path, method=method, headers=headers(),
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.status, json.loads(r.read() or b"{}")
def startup_done():
    try:
        return bool(api("GET", "/process/startup-status")[1].get("terminal"))
    except Exception:
        return False
if os.environ.get("WAIT") == "1":
    for _ in range(120):  # up to 20 min
        if startup_done():
            break
        time.sleep(10)
    else:
        print("DUMB startup didn't finish in 20 min; not touching", link)
        sys.exit(1)
elif not startup_done():
    print("DUMB is still starting; not touching", link, "(rerun later)")
    sys.exit(1)
if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(real):
    sys.exit(0)
if not os.path.exists(os.path.join(real, "settings.json")):
    print("no settings in", real, "- leaving", link, "alone")
    sys.exit(0)
def call(action):
    status, _ = api("POST", f"/process/{action}-service", {"process_name": "Riven Backend"})
    print(f"{action} Riven Backend:", status)
print(link, "is not the symlink to", real, "- relinking")
call("stop")
if os.path.lexists(link):
    os.rename(link, link + ".stray-" + time.strftime("%Y%m%d-%H%M%S"))
os.symlink(real, link)
st = os.stat(real)
os.lchown(link, st.st_uid, st.st_gid)
call("start")
PY
