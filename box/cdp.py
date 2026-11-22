"""A private DevTools channel to the box's Chromium, for facts a page must not be able to fake.

Chromium is started with --remote-debugging-pipe: it reads commands on fd 3 and writes replies on
fd 4. entrypoint.sh wires those to two FIFOs in /run/ghostbox, a folder only the ghost user can
open, so this channel is reachable from boxd and from nothing a page can touch: no port, no
clipboard, no keystrokes. (A page cannot see it either: no Runtime.enable, no console hooks.)

What boxd asks it, from an isolated JavaScript world (the page's scripts cannot redefine what
that world sees):
    front()     -> the tab the user is looking at: origin, URL, and the focused element
                   (tag, type, readOnly, whether focus sits inside an iframe)
    navigate()  -> open a URL in that tab as a typed navigation (a page cannot block it)
"""
import json
import os
import select
import threading
import time

PIPE_IN = os.environ.get("BOX_CDP_IN", "/run/ghostbox-cdp/in")     # we write commands here
PIPE_OUT = os.environ.get("BOX_CDP_OUT", "/run/ghostbox-cdp/out")  # Chromium writes replies here

_PROBE = r"""(() => {
  const a = document.activeElement;
  return JSON.stringify({
    origin: location.origin, href: location.href,
    visible: document.visibilityState === 'visible', focused: document.hasFocus(),
    tag: a ? a.tagName : null, type: a && 'type' in a ? String(a.type) : null,
    readOnly: !!(a && (a.readOnly || a.disabled)),
    autocomplete: a && a.getAttribute ? a.getAttribute('autocomplete') : null,
    inFrame: !!(a && (a.tagName === 'IFRAME' || a.tagName === 'FRAME')),
  });
})()"""


class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, pin=PIPE_IN, pout=PIPE_OUT):
        self.pin, self.pout = pin, pout
        self._w = self._r = None
        self._id = 0
        self._lock = threading.Lock()

    def _open(self):
        # Chromium holds both FIFOs open read-write (entrypoint.sh), so these opens do not block
        # while it runs; when it is restarting they wait, hence the helper thread and deadline.
        box = {}

        def go():
            try:
                box["w"] = open(self.pin, "wb", buffering=0)
                box["r"] = open(self.pout, "rb", buffering=0)
            except OSError as e:
                box["e"] = e
        t = threading.Thread(target=go, daemon=True); t.start(); t.join(10)
        if "r" not in box:
            raise CDPError("browser control pipe not available (%s)" % box.get("e", "timeout"))
        self._w, self._r, self._buf = box["w"], box["r"], b""

    def _reset(self):
        for f in (self._w, self._r):
            try:
                f and f.close()
            except OSError:
                pass
        self._w = self._r = None

    def call(self, method, params=None, session=None, timeout=10):
        with self._lock:
            for attempt in (1, 2):          # Chromium restarts reopen the pipe: retry once
                try:
                    if self._w is None:
                        self._open()
                    self._id += 1
                    msg = {"id": self._id, "method": method, "params": params or {}}
                    if session:
                        msg["sessionId"] = session
                    self._w.write(json.dumps(msg).encode() + b"\0")
                    return self._read(self._id, timeout)
                except (OSError, CDPError) as e:
                    self._reset()
                    if attempt == 2:
                        raise CDPError("browser control: %s" % e)

    def _read(self, want, timeout):
        end = time.time() + timeout
        while time.time() < end:
            while b"\0" in self._buf:
                raw, self._buf = self._buf.split(b"\0", 1)
                m = json.loads(raw)
                if m.get("id") == want:
                    if "error" in m:
                        raise CDPError(m["error"].get("message", "error"))
                    return m.get("result", {})
            ready, _, _ = select.select([self._r], [], [], max(0.05, end - time.time()))
            if not ready:
                continue
            chunk = os.read(self._r.fileno(), 65536)
            if not chunk:
                raise CDPError("pipe closed")
            self._buf += chunk
        raise CDPError("no reply to request %d" % want)

    def _attach(self, target_id):
        return self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]

    def _detach(self, s):
        try:
            self.call("Target.detachFromTarget", {"sessionId": s})
        except CDPError:
            pass

    def _frame(self, s):
        f = self.call("Page.getFrameTree", session=s)["frameTree"]["frame"]
        return f["id"], f.get("loaderId")

    def _world(self, s):
        frame, _ = self._frame(s)
        return self.call("Page.createIsolatedWorld", {"frameId": frame, "worldName": "ghostbox"},
                         session=s)["executionContextId"]

    def _eval(self, s, ctx, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "contextId": ctx, "returnByValue": True}, session=s)
        if "exceptionDetails" in r:
            raise CDPError("probe failed")
        return r["result"].get("value")

    def _probe(self, target_id):
        s = self._attach(target_id)
        try:
            return json.loads(self._eval(s, self._world(s), _PROBE))
        finally:
            self._detach(s)

    def front(self, strict=False):
        """The tab in front. strict (for secrets): every page must answer and exactly one may be
        both visible and focused; otherwise refuse. Not strict (navigation): the one visible tab."""
        pages = [t for t in self.call("Target.getTargets")["targetInfos"] if t.get("type") == "page"]
        seen = []
        for t in pages:
            try:
                p = self._probe(t["targetId"])
            except CDPError:
                if strict:
                    raise CDPError("a tab did not answer; refusing")
                continue
            p["targetId"] = t["targetId"]
            if p.get("visible"):
                seen.append(p)
        pick = [p for p in seen if p.get("focused")] if strict else seen
        if len(pick) != 1:
            raise CDPError("cannot tell which tab is in front (%d candidates)" % len(pick))
        return pick[0]

    def insert_secret(self, target_id, origin, types, value):
        """Check and type in one session on one tab: the tab's origin must equal origin and its
        focused element must be a writable top-level input whose type is in types; then select
        its contents and insert value there (Input.insertText goes to that tab's focused element
        only, not through the X display). Nothing is inserted if any check fails."""
        s = self._attach(target_id)
        try:
            _, loader = self._frame(s)
            ctx = self._world(s)
            p = json.loads(self._eval(s, ctx, _PROBE))
            if p.get("origin") != origin:
                raise PermissionError("refused: the page is %s, not %s" % (p.get("origin"), origin))
            if p.get("inFrame") or p.get("tag") != "INPUT" or p.get("readOnly") or not p.get("focused") \
                    or (p.get("type") or "").lower() not in types:
                raise PermissionError("refused: focus is on %s type=%s" % (p.get("tag"), p.get("type")))
            # Clear the field from the isolated world (fires no page event, unlike select()), mark
            # the element we checked, insert, then confirm the same document and element.
            self._eval(s, ctx, "(() => { const a = document.activeElement; a.value = ''; "
                               "window.__ghostboxEl = a; return true; })()")
            self.call("Input.insertText", {"text": value}, session=s)
            _, loader2 = self._frame(s)
            same = self._eval(s, ctx, "document.activeElement === window.__ghostboxEl && "
                                      "location.origin === %s" % json.dumps(origin))
            self._eval(s, ctx, "delete window.__ghostboxEl, true")
            if loader2 != loader or same is not True:
                try:
                    self._eval(s, ctx, "(() => { const a = document.activeElement; "
                                       "if (a && 'value' in a) a.value = ''; return true; })()")
                except CDPError:
                    pass
                raise PermissionError("the page changed while the secret went in; cleared and stopped")
            return p["origin"]
        finally:
            self._detach(s)

    def navigate(self, url):
        """Open url in the front tab as a typed navigation (what the address bar does). A page
        can intercept keystrokes such as Ctrl+L, but not this."""
        tid = self.front()["targetId"]
        s = self.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        try:
            r = self.call("Page.navigate", {"url": url, "transitionType": "typed"}, session=s, timeout=30)
            if r.get("errorText"):
                raise CDPError(r["errorText"])
        finally:
            try:
                self.call("Target.detachFromTarget", {"sessionId": s})
            except CDPError:
                pass
