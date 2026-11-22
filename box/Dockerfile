# ghostbox: ghosthands on a Linux desktop in a container, no hardware.
# Build: docker build -f box/Dockerfile -t ghostbox .      (from the repo root)
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 TZ=UTC
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11-utils x11-xserver-utils openbox xdotool xclip x11vnc novnc python3-websockify \
      chromium libgl1-mesa-dri libegl1 libgles2 mesa-vulkan-drivers fonts-liberation fonts-noto-core fonts-noto-color-emoji fonts-dejavu-core \
      python3 python3-xlib python3-pil python3-serial \
      locales tzdata ca-certificates dbus-x11 \
 && sed -i 's/# en_AU.UTF-8/en_AU.UTF-8/; s/# en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen \
 && ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html \
 && rm -rf /var/lib/apt/lists/*

# Two unprivileged users that cannot read each other (see entrypoint.sh). Root keeps only the
# start-up script and the loop that restarts Chromium (it opens the DevTools pipes).
RUN useradd -m -u 1000 -s /bin/bash ghost && useradd -m -u 1001 -s /usr/sbin/nologin browser \
 && usermod -aG browser ghost && mkdir -p /data

COPY box/openbox.xml /etc/ghostbox/openbox.xml
# Downloads land in /data/downloads unasked; no password manager or autofill of the browser's own.
COPY box/chromium-policy.json /etc/chromium/policies/managed/ghostbox.json
# The vault extension and its native-messaging host (see box/ext/bg.js).
COPY box/ext /opt/ghostbox/ext
COPY box/nmhost.json /etc/chromium/native-messaging-hosts/dev.ghostbox.vault.json
COPY box/boxd.py box/extlink.py box/nmhost.py box/jobs.py box/entrypoint.sh /opt/ghostbox/
COPY box/testpage.html /opt/ghostbox/www/index.html
COPY box/phish.html /opt/ghostbox/www/phish.html
# The agent itself, so a whole run can live in the container: docker exec -u ghost ghostbox gh-run ...
COPY ghosthands /opt/ghosthands/ghosthands
COPY run.py /opt/ghosthands/run.py
COPY examples /opt/ghosthands/examples
RUN chmod +x /opt/ghostbox/entrypoint.sh /opt/ghostbox/nmhost.py \
 && printf '#!/bin/sh\ncd /opt/ghosthands && exec python3 run.py --target box "$@"\n' > /usr/local/bin/gh-run \
 && chmod +x /usr/local/bin/gh-run

ENV DISPLAY=:99 BOX_PORT=8780 GH_BOX_URL=http://127.0.0.1:8780 GH_RUNS_DIR=/data/runs
VOLUME /data
# No EXPOSE: ports are published explicitly on 127.0.0.1 (compose.yml), so `docker run -P` opens nothing.
HEALTHCHECK --interval=10s --timeout=3s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8780/health')"
ENTRYPOINT ["/opt/ghostbox/entrypoint.sh"]
