#!/bin/bash
# ghosthands screen feed -- the agent's "eyes".
# macOS: MUST run inside a GUI Terminal (Screen Recording permission does not attach to an
# SSH-spawned process). Children of Terminal.app inherit its grant.
#
# Env:
#   GH_FRAME       output jpg           (default /tmp/gh_frame.jpg)
#   GH_TRIGGER     trigger file         (default /tmp/gh_capture_now)
#   GH_MAX_WIDTH   downscale max width  (default 1600; 0 = native res)
#   GH_FEED_MODE   trigger | continuous (default trigger)
#   GH_FEED_FPS    fps in continuous    (default 4)
#   GH_DISPLAY     screencapture -D id  (default main display)
FRAME="${GH_FRAME:-/tmp/gh_frame.jpg}"
TRIGGER="${GH_TRIGGER:-/tmp/gh_capture_now}"
MAX_W="${GH_MAX_WIDTH:-1600}"
MODE="${GH_FEED_MODE:-trigger}"
FPS="${GH_FEED_FPS:-4}"
TMP="${FRAME%.jpg}_tmp.jpg"
DFLAG=""; [ -n "$GH_DISPLAY" ] && DFLAG="-D $GH_DISPLAY"

grab() {
  if screencapture -x -t jpg $DFLAG "$TMP" 2>/dev/null; then
    if [ "$MAX_W" -gt 0 ] 2>/dev/null; then
      sips -Z "$MAX_W" "$TMP" --out "$TMP" >/dev/null 2>&1
    fi
    mv -f "$TMP" "$FRAME"
    return 0
  fi
  return 1
}

echo "ghosthands feed  mode=$MODE  frame=$FRAME  maxw=$MAX_W  fps=$FPS"
echo "Leave this window open. Ctrl-C to stop."
if [ "$MODE" = "continuous" ]; then
  DELAY=$(awk "BEGIN{print 1/$FPS}")
  while true; do grab; sleep "$DELAY"; done
else
  # tight trigger loop: ~20ms response, 0% idle CPU
  while true; do
    if [ -f "$TRIGGER" ]; then rm -f "$TRIGGER"; grab && echo "  frame $(date +%H:%M:%S)"; fi
    sleep 0.02
  done
fi
