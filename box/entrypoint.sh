#!/usr/bin/env bash
# ghostbox: Xvfb screen + openbox + headed Chromium + VNC/noVNC viewer + boxd control daemon.
#
# Starts as root only to set up the volume and drop to two users that cannot read each other:
#   ghost   (1000)  X server, VNC, boxd, job worker, the agent; holds the API key and box token
#   browser (1001)  Chromium, started with an empty environment
# A page that breaks out of Chromium's renderer lands as `browser`: it cannot read ghost's
# /proc/*/environ, the token, the VNC password or the job files.
set -euo pipefail

: "${BOX_W:=1440}" "${BOX_H:=900}" "${DISPLAY:=:99}"
export DISPLAY
# The control API always needs a token; the in-box job worker reads it as GH_BOX_TOKEN.
: "${BOX_TOKEN:=$(python3 -c 'import secrets;print(secrets.token_hex(16))')}"
export BOX_TOKEN GH_BOX_TOKEN="$BOX_TOKEN"

as() {  # as USER cmd... : drop to USER, no capabilities left
  local u="$1"; shift
  setpriv --reuid="$u" --regid="$u" --init-groups --inh-caps=-all "$@"
}

PROFILE=/data/profile
install -d -m 755 /data
install -d -m 700 -o ghost -g ghost /data/jobs /data/runs /data/vault
install -d -m 700 -o browser -g browser "$PROFILE"
# Downloads: the browser writes, the job worker (ghost, in group browser) reads.
install -d -m 750 -o browser -g browser /data/downloads
# Volumes from before the split held the profile as ghost.
[ "$(stat -c %u "$PROFILE")" = 1001 ] && [ -z "$(find "$PROFILE" -maxdepth 2 ! -user browser -print -quit)" ] \
  || chown -R browser:browser "$PROFILE" /data/downloads
# Chromium's lock links name the last container's hostname and PID; PIDs restart in every
# container, so a stale lock can look alive. Only this container uses the profile: drop them.
rm -f "$PROFILE"/SingletonLock "$PROFILE"/SingletonSocket "$PROFILE"/SingletonCookie
# Stale X lock from a previous run of this container
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
install -d -m 1777 /tmp/.X11-unix

as ghost Xvfb "$DISPLAY" -screen 0 "${BOX_W}x${BOX_H}x24" -dpi 96 -nolisten tcp +extension XTEST &
for _ in $(seq 50); do as ghost xdpyinfo >/dev/null 2>&1 && break; sleep 0.1; done
# The browser user may draw on the screen and nothing more.
as ghost xhost +SI:localuser:browser >/dev/null

as ghost env HOME=/home/ghost openbox --config-file /etc/ghostbox/openbox.xml &

# Viewer: VNC on 5900 (loopback), noVNC on 6080. Watch-only unless BOX_VNC_INPUT=1, so a stray
# click from whoever is watching cannot steer a run. Always a password (the box's own browser can
# reach localhost:6080): BOX_VNC_PASSWORD or a random one, kept where only ghost can read it.
: "${BOX_VNC_PASSWORD:=$(python3 -c 'import secrets;print(secrets.token_urlsafe(6)[:8])')}"
install -d -m 700 -o ghost -g ghost /run/ghostbox
printf '%s\n' "${BOX_VNC_PASSWORD:0:8}" > /run/ghostbox/vnc-password
as ghost x11vnc -storepasswd "${BOX_VNC_PASSWORD:0:8}" /run/ghostbox/vncpass >/dev/null 2>&1
chown ghost:ghost /run/ghostbox/vnc-password; chmod 600 /run/ghostbox/vnc-password
VNC_ARGS=(-display "$DISPLAY" -forever -shared -quiet -noxdamage -localhost -rfbauth /run/ghostbox/vncpass)
[ "${BOX_VNC_INPUT:-0}" = 1 ] || VNC_ARGS+=(-viewonly)
as ghost x11vnc "${VNC_ARGS[@]}" >/tmp/x11vnc.log 2>&1 &
as ghost websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

# Vault extension link (box/ext, box/nmhost.py, box/extlink.py): boxd listens on a socket in this
# folder; only the browser user (group) may reach it, and boxd checks the caller's uid as well.
install -d -m 750 -o ghost -g browser /run/ghostbox-ext

# Preferred languages (navigator.languages, Accept-Language) to match BOX_LANG, as a person's
# browser would; --lang alone only sets the browser's own interface language.
L="${BOX_LANG:-en-US}"
printf '{"ForcedLanguages": ["%s", "%s"]}\n' "$L" "${L%%-*}" > /etc/chromium/policies/managed/languages.json

# Headed Chromium, restarted if it is closed. No --headless, no remote debugging (port or pipe),
# no --enable-automation, so navigator.webdriver is false: from the page's side this is an
# ordinary desktop browser with one extension installed.
CHROME_FLAGS=(--user-data-dir="$PROFILE" --no-first-run --no-default-browser-check
  --password-store=basic --window-position=0,0 --window-size="${BOX_W},${BOX_H}" --start-maximized
  --disable-dev-shm-usage --hide-crash-restore-bubble --lang="${BOX_LANG:-en-US}" --download-default-directory=/data/downloads
  --load-extension=/opt/ghostbox/ext --disable-features=DisableLoadExtensionCommandLineSwitch)
# Chromium's own sandbox needs user namespaces: compose.yml's seccomp profile allows them. On a
# runtime without that profile, BOX_CHROME_SANDBOX=0 falls back to --no-sandbox (the user split
# and the container are then the only sandbox).
[ "${BOX_CHROME_SANDBOX:-1}" = 1 ] || CHROME_FLAGS+=(--no-sandbox --test-type)
CHROME_ENV=(env -i DISPLAY="$DISPLAY" HOME=/home/browser PATH=/usr/bin:/bin LANG="$LANG" LC_ALL="${LC_ALL:-$LANG}" TZ="${TZ:-UTC}")
( while true; do
    as browser "${CHROME_ENV[@]}" chromium "${CHROME_FLAGS[@]}" "${BOX_START_URL:-about:blank}" \
      >/tmp/chromium.log 2>&1 || true
    rm -f "$PROFILE"/SingletonLock "$PROFILE"/SingletonSocket "$PROFILE"/SingletonCookie; sleep 1
  done ) &

# Local input-check page for box/smoke.py (loopback inside the container only)
as ghost python3 -m http.server 8781 --bind 127.0.0.1 -d /opt/ghostbox/www >/dev/null 2>&1 &

as ghost env HOME=/home/ghost python3 /opt/ghostbox/jobs.py worker >>/data/jobs/worker.log 2>&1 &

exec setpriv --reuid=ghost --regid=ghost --init-groups --inh-caps=-all env HOME=/home/ghost python3 /opt/ghostbox/boxd.py
