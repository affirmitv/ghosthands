"""ghosthands.dom_reader — read the front Safari tab as an indexed element table.

Read-only observation path: uses Safari's AppleScript `do JavaScript` bridge
(no WebDriver, page never sees automation). Requires Safari > Develop >
"Allow JavaScript from Apple Events". Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from typing import Optional

SNAPSHOT_JS = r"""(() => {
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,' +
    '[role=button],[role=link],[role=textbox],[role=combobox],[role=checkbox],' +
    '[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],' +
    '[role=searchbox],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  const vw = window.innerWidth, vh = window.innerHeight;
  const vis = el => {
    const r = el.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) return null;
    if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) return null;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity <= 0) return null;
    if (el.disabled) return null;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const hit = document.elementFromPoint(cx, cy);
    if (hit && !(el === hit || el.contains(hit) || hit.contains(el))) return null;
    return r;
  };
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const name = el => {
    let n = txt(el.getAttribute('aria-label'));
    if (!n) {
      const lb = el.getAttribute('aria-labelledby');
      if (lb) n = txt((document.getElementById(lb) || {}).innerText);
    }
    if (!n && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')) {
      if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) n = txt(l.innerText); }
      if (!n) { const p = el.closest('label'); if (p) n = txt(p.innerText); }
    }
    if (!n) n = txt(el.getAttribute('placeholder'));
    if (!n) n = txt(el.getAttribute('title'));
    if (!n) { const im = el.querySelector('img[alt]'); if (im) n = txt(im.alt); }
    if (!n && el.tagName === 'INPUT' && /^(submit|button|image)$/i.test(el.type || '')) n = txt(el.value);
    if (!n) n = txt(el.innerText || el.textContent);
    return n.slice(0, 80);
  };
  const role = el => {
    const r = el.getAttribute('role');
    if (r) return r;
    const t = el.tagName;
    if (t === 'A') return 'link';
    if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'SELECT') return 'combobox';
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'INPUT') {
      const ty = (el.type || '').toLowerCase();
      if (ty === 'checkbox' || ty === 'radio' || ty === 'submit' || ty === 'button' || ty === 'image') return 'button';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'button';
  };
  const val = el => {
    const t = el.tagName;
    if (t === 'INPUT' && /^(checkbox|radio)$/i.test(el.type || '')) return el.checked ? 'checked' : 'unchecked';
    if (t === 'SELECT') { const o = el.selectedOptions && el.selectedOptions[0]; return txt(o ? o.textContent : '').slice(0, 60); }
    if (t === 'INPUT' || t === 'TEXTAREA') return txt(el.value).slice(0, 60);
    return '';
  };
  const seen = [], out = [];
  document.querySelectorAll(SEL).forEach(el => {
    const r = vis(el);
    if (!r) return;
    if (seen.some(s => Math.abs(s[0] - r.top) < 2 && Math.abs(s[1] - r.left) < 2)) return;
    seen.push([r.top, r.left]);
    out.push({label: name(el), role: role(el), value: val(el),
      x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)});
  });
  out.sort((a, b) => a.y - b.y || a.x - b.x);
  out.length = Math.min(out.length, 80);
  return JSON.stringify({
    title: document.title, url: location.href,
    text: txt(document.body ? document.body.innerText : '').slice(0, 1500),
    viewport: {w: vw, h: vh, sx: window.screenX, sy: window.screenY,
               ow: window.outerWidth, oh: window.outerHeight,
               sw: (screen && screen.width) || 0, sh: (screen && screen.height) || 0},
    elements: out
  });
})()"""

_HINT = ("Enable Safari > Develop > Allow JavaScript from Apple Events, or run: "
         "defaults write com.apple.Safari AllowJavaScriptFromAppleEvents -bool true "
         "then restart Safari.")


class SafariReaderError(RuntimeError):
    """Raised when reading or driving Safari via AppleScript fails."""


def _osascript(script: str, timeout: float = 15.0) -> str:
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        err = p.stderr.strip()
        low = err.lower()
        if "allow javascript from apple events" in low or "not allowed" in low:
            err = err + "\nHINT: " + _HINT
        raise SafariReaderError(err)
    return p.stdout.strip()


def _as_string_literal(s: str) -> str:
    """Escape a Python string into an AppleScript double-quoted literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_safari_js(js: str, timeout: float = 15.0) -> str:
    """Run JS in the front Safari tab via `do JavaScript`; return its string result."""
    script = ('tell application "Safari" to do JavaScript ' + _as_string_literal(js) +
              ' in current tab of front window')
    return _osascript(script, timeout)


_screen_cache: tuple[float, Optional[tuple[int, int]]] = (0.0, None)


def screen_size() -> tuple[int, int]:
    """Return the main screen size in points (W, H), cached for 60 s.

    Fallback only: SafariReader prefers the page's own ``screen.width/height``
    (same display Safari is on, and Finder can time out over ssh)."""
    global _screen_cache
    ts, cached = _screen_cache
    if cached is not None and time.time() - ts < 60.0:
        return cached
    out = _osascript('tell application "Finder" to get bounds of window of desktop')
    parts = [p.strip() for p in out.split(",")]
    if len(parts) != 4:
        raise SafariReaderError("unexpected Finder bounds: %r" % out)
    size = (int(parts[2]), int(parts[3]))
    _screen_cache = (time.time(), size)
    return size


class Element:
    """One interactive control, indexed, with viewport-rect coordinates."""

    def __init__(self, index: str, label: str, role: str, value: str,
                 x: float, y: float, w: float, h: float) -> None:
        self.index, self.label, self.role, self.value = index, label, role, value
        self.x, self.y, self.w, self.h = x, y, w, h

    def screen_point(self, viewport: dict, screen: tuple[int, int]) -> tuple[float, float]:
        """Element center as (fx, fy) fractions of the screen in [0, 1]."""
        chrome = viewport.get("oh", viewport["h"]) - viewport["h"]
        sx = viewport.get("sx", 0) + self.x + self.w / 2.0
        sy = viewport.get("sy", 0) + chrome + self.y + self.h / 2.0
        fx = min(1.0, max(0.0, sx / max(1, screen[0])))
        fy = min(1.0, max(0.0, sy / max(1, screen[1])))
        return (fx, fy)

    def operations(self) -> list[str]:
        """Operations the hands can perform on this element."""
        if self.role in ("textbox", "combobox", "searchbox"):
            return ["TYPE_TEXT", "CLICK"]
        return ["CLICK"]


class Screen:
    """A snapshot of the front Safari tab: metadata plus indexed elements."""

    def __init__(self, title: str, url: str, text: str, viewport: dict,
                 elements: list[Element], screen: tuple[int, int]) -> None:
        self.title, self.url, self.text = title, url, text
        self.viewport, self.elements, self.screen = viewport, elements, screen

    @classmethod
    def from_json(cls, data: dict, screen: tuple[int, int]) -> "Screen":
        els = [Element(str(i + 1), e.get("label", ""), e.get("role", ""),
                       e.get("value", ""), e["x"], e["y"], e["w"], e["h"])
               for i, e in enumerate(data.get("elements", []))]
        return cls(data.get("title", ""), data.get("url", ""), data.get("text", ""),
                   data.get("viewport", {}), els, screen)

    def by_index(self, idx: str) -> Element:
        for e in self.elements:
            if e.index == idx:
                return e
        raise KeyError(idx)

    def table(self) -> str:
        lines = []
        for e in self.elements:
            v = e.value if e.value else "empty"
            lines.append("[%s] %s  %s  · %s" % (e.index, e.role, e.label, v))
        return "\n".join(lines)

    def to_jev_state(self, recent_actions: list[str]) -> dict:
        return {
            "page": {"title": self.title, "url": self.url, "text": self.text},
            "elements": [{"index": e.index, "label": e.label, "role": e.role,
                          "value": e.value, "operations": e.operations()}
                         for e in self.elements],
            "recent_actions": recent_actions[-8:],
        }

    def fingerprint(self) -> str:
        h = hashlib.sha1()
        h.update(self.url.encode("utf-8", "replace"))
        for e in sorted(self.elements, key=lambda e: e.index):
            row = "%s|%s|%s|%d|%d" % (e.label, e.role, e.value, round(e.x), round(e.y))
            h.update(row.encode("utf-8", "replace"))
        return h.hexdigest()


class SafariReader:
    """Reads the front Safari tab through the AppleScript JS bridge."""

    def snapshot(self) -> Screen:
        """Return a Screen for the front tab; retries once on a JSON error."""
        try:
            data = json.loads(run_safari_js(SNAPSHOT_JS))
        except (json.JSONDecodeError, TypeError):
            time.sleep(0.4)
            try:
                data = json.loads(run_safari_js(SNAPSHOT_JS))
            except (json.JSONDecodeError, TypeError) as e:
                raise SafariReaderError("Safari returned non-JSON (mid-navigation?): %s" % e)
        vp = data.get("viewport") or {}
        if vp.get("sw") and vp.get("sh"):
            size = (int(vp["sw"]), int(vp["sh"]))
        else:
            size = screen_size()
        return Screen.from_json(data, size)

    def navigate(self, url: str) -> None:
        """Point the front tab at `url`."""
        _osascript('tell application "Safari" to set URL of current tab of front window to '
                   + _as_string_literal(url))
