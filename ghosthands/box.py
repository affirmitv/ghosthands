"""ghostbox target: eyes and hands for a Linux desktop in a Docker container, no hardware.

box/ builds a container with an Xvfb screen, a headed Chromium and a small control daemon
(box/boxd.py). This module is the client: it keeps ghosthands' brain and grounder and swaps only
the I/O, the same way the terminal-mac target does.

    BoxEyes   capture()   -> POST /action {"verb": "screenshot"}  (JPEG, size in headers)
    BoxHands  move/click  -> {"verb": "move" | "click", x, y}     (screen pixels)
              type/key    -> {"verb": "type" | "key", ...}
              scroll      -> {"verb": "scroll", "amount": notches}
              navigate    -> {"verb": "navigate", "url": ...}

Input inside the box goes through XTEST with human motion and typing timing (see boxd.py), so
the browser gets trusted events. Stdlib only.
"""
import json
import os
import tempfile
import urllib.error
import urllib.request

from .config import Config
from .hands import Hands


class BoxError(RuntimeError):
    pass


class Box:
    def __init__(self, url=None, token=None, timeout=None):
        self.url = (url or Config.box_url).rstrip("/")
        self.token = token if token is not None else Config.box_token
        self.timeout = float(timeout or Config.box_timeout)

    def _post(self, payload):
        req = urllib.request.Request(self.url + "/action", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        if self.token:
            req.add_header("X-Box-Token", self.token)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:300]
            raise BoxError("box %s failed (%d): %s" % (payload.get("verb"), e.code, msg))
        except (urllib.error.URLError, OSError) as e:
            raise BoxError("box not reachable at %s (%s); start it: docker compose -f box/compose.yml up -d"
                           % (self.url, e))

    def action(self, verb, **kw):
        with self._post({"verb": verb, **kw}) as r:
            body = r.read()
        return json.loads(body) if body else {}

    def screenshot(self, path):
        with self._post({"verb": "screenshot"}) as r:
            data = r.read()
            w, h = int(r.headers.get("X-Width", 0)), int(r.headers.get("X-Height", 0))
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return w, h


class BoxEyes:
    """Drop-in for ghosthands.eyes.Eyes; the size comes from the reply, so no sips/PIL needed."""

    def __init__(self, box=None, frame=None):
        self.box = box or Box()
        self.frame = frame or os.path.join(tempfile.gettempdir(), "gh_box_frame.jpg")
        self._dims = None

    def capture(self, timeout=None):
        self._dims = self.box.screenshot(self.frame)
        return self.frame

    def dims(self, path=None):
        if not self._dims:
            self.capture()
        return self._dims


class BoxHands(Hands):
    """Coordinates arrive as screen fractions (the ghosthands contract) and become box pixels."""

    def __init__(self, box=None):
        self.box = box or Box()
        s = self.box.action("status")
        self._screen = (int(s.get("width", 1440)), int(s.get("height", 900)))
        self._pos = (int(s.get("x", 0)), int(s.get("y", 0)))

    def _xy(self, x01, y01):
        w, h = self._screen
        x = int(round(min(1.0, max(0.0, float(x01))) * w))
        y = int(round(min(1.0, max(0.0, float(y01))) * h))
        return min(x, w - 1), min(y, h - 1)

    def move(self, x01, y01):
        self._pos = self._xy(x01, y01)
        self.box.action("move", x=self._pos[0], y=self._pos[1])
        return "ok"

    def click(self, button="left"):
        self.box.action("click", button=button)
        return "ok"

    def double_click(self):
        self.box.action("click", double=True)
        return "ok"

    def type(self, text):
        if text:
            self.box.action("type", text=text)
        return "ok"

    def key(self, keys):
        if keys:
            self.box.action("key", keys=keys)   # boxd maps Mac names (cmd, delete) to Linux ones
        return "ok"

    def scroll(self, amount):
        if isinstance(amount, dict):
            amount = amount.get("amount", 0)
        if int(amount):
            self.box.action("scroll", amount=int(amount))
        return "ok"

    def navigate(self, url):
        self.box.action("navigate", url=url)
        return "ok"

    def vault(self):
        """Logins in the box's vault: [{name, site, user, totp}], never values."""
        return self.box.action("vault").get("logins", [])

    def fill_secret(self, name, field="password", x01=None, y01=None):
        """Click the field (screen fractions) and type a vault value there; the box refuses
        unless its front tab is on that login's site."""
        kw = {"name": name, "field": field}
        if x01 is not None and y01 is not None:
            self._pos = self._xy(x01, y01)
            kw.update(x=self._pos[0], y=self._pos[1])
        return self.box.action("fill_secret", **kw)

    def downloads(self):
        """{"done": [[name, bytes]], "pending": [name]} for the box's download folder."""
        return self.box.action("downloads")


def make_box(url=None):
    """(eyes, hands) sharing one Box client."""
    box = Box(url)
    return BoxEyes(box), BoxHands(box)
