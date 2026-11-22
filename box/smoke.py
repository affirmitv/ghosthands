#!/usr/bin/env python3
"""ghostbox smoke test, no LLM: drive a local test page through the box and save screenshots.

The page (served from inside the container on 127.0.0.1:8781) prints each mouse, key and wheel
event with event.isTrusted and the pointer's path length, so one screenshot shows whether the
browser took the input as a real device's.

    python3 box/smoke.py [--url http://127.0.0.1:8780] [--out DIR]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ghosthands.box import Box, BoxEyes, BoxHands  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--url", default=None)
ap.add_argument("--out", default="ghostbox-smoke")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)

box = Box(a.url, token=os.environ.get("GH_BOX_TOKEN"))
eyes, hands = BoxEyes(box, os.path.join(a.out, "frame.jpg")), BoxHands(box)
w, h = hands._screen
print("box screen %dx%d" % (w, h))


def shot(name):
    eyes.capture()
    os.replace(eyes.frame, os.path.join(a.out, name))
    print("saved", os.path.join(a.out, name))


hands.navigate("http://127.0.0.1:8781/")
time.sleep(2.5)
shot("1-page.jpg")
# Screen fractions of the test page's text box and button (the browser toolbar is ~87 px tall).
hands.move(0.25, 0.458); hands.click(); time.sleep(0.3)
hands.type("Hello from ghostbox!")
hands.key("cmd+a")
hands.move(0.25, 0.592); hands.click(); time.sleep(0.3)
hands.move(0.6, 0.6); hands.scroll(2); time.sleep(0.5)
shot("2-after-input.jpg")
print("ok")
