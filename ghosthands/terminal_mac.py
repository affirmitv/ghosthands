"""terminal-mac target: eyes and hands for a separate macOS guest VM.

terminal-mac (https://github.com/govijr/terminal-mac, MIT) runs a real macOS guest under Apple
Virtualization. A helper inside the guest takes screenshots and posts input there, and the host CLI
talks to it over a shared-folder mailbox. Nothing here touches the host's own screen, pointer,
keyboard or clipboard.

ghosthands keeps its brain (planner) and its eyes->coords (grounder) and only swaps the I/O:

    TerminalMacEyes   capture()  -> `terminal-mac action NAME screenshot --output ...`
    TerminalMacHands  move/click -> `terminal-mac action NAME click X Y`
                      type/key   -> `terminal-mac action NAME type ... | key ...`
                      scroll     -> `terminal-mac action NAME scroll --dy ...`

The same task file runs against a real Mac (screenfeed eyes + Pico hands) or a terminal-mac guest;
only `--target` changes. Every call is one argv list to the CLI: no shell, nothing interpolated.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

from .config import Config
from .hands import Hands


class TerminalMacError(RuntimeError):
    pass


class TerminalMac:
    """Thin client for the `terminal-mac action NAME ...` CLI. Each call returns the parsed JSON
    reply; a nonzero exit or an unparseable reply raises TerminalMacError with the CLI's message."""

    def __init__(self, name=None, binary=None, timeout=None):
        self.name = name or Config.tmac_vm
        self.binary = binary or Config.tmac_bin or shutil.which("terminal-mac") \
            or os.path.expanduser("~/.local/bin/terminal-mac")
        if not os.path.exists(self.binary):
            raise TerminalMacError(
                "terminal-mac not found (%s). Install it: curl -fsSL "
                "https://github.com/govijr/terminal-mac/releases/latest/download/get.sh | bash"
                % self.binary)
        self.timeout = int(timeout or Config.tmac_timeout)

    def action(self, verb, *args):
        # Options first: everything after a `--` in args is literal text.
        argv = [self.binary, "action", self.name, verb, "--timeout", str(self.timeout),
                *[str(a) for a in args]]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout + 10)
        except subprocess.TimeoutExpired:
            raise TerminalMacError("terminal-mac %s timed out after %ds" % (verb, self.timeout))
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            raise TerminalMacError("terminal-mac %s failed: %s" % (verb, (r.stderr or out).strip()[:400]))
        try:
            return json.loads(out) if out else {}
        except ValueError:
            raise TerminalMacError("terminal-mac %s returned non-JSON: %r" % (verb, out[:200]))

    def status(self):
        """Guest helper status: screenRecording, accessibility, width, height (guest points)."""
        return self.action("status")

    def require_ready(self):
        s = self.status()
        missing = [k for k in ("screenRecording", "accessibility") if str(s.get(k)).lower() != "true"]
        if missing:
            raise TerminalMacError(
                "guest helper is missing %s; open TerminalMacGuest.app inside the VM and grant it"
                % " and ".join(missing))
        return s


class TerminalMacEyes:
    """Drop-in for ghosthands.eyes.Eyes. A screenshot is in guest points (a 1280x800 guest gives a
    1280x800 image on a 2x display too), so grounded fractions map straight back to click points.
    Frames are re-encoded as JPEG because the planner sends image/jpeg."""

    def __init__(self, vm=None, frame=None):
        self.vm = vm or TerminalMac()
        self.frame = frame or os.path.join(tempfile.gettempdir(), "gh_tmac_%s.jpg" % self.vm.name)
        self._png = self.frame[:-4] + ".png"
        self._dims = None

    def capture(self, timeout=None):
        reply = self.vm.action("screenshot", "--output", self._png)
        png = reply.get("image") or self._png
        r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85",
                            png, "--out", self.frame], capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not os.path.exists(self.frame):
            raise TerminalMacError("could not convert guest screenshot: %s" % r.stderr.strip()[:200])
        if reply.get("width") and reply.get("height"):
            self._dims = (int(reply["width"]), int(reply["height"]))
        return self.frame

    def dims(self, path=None):
        if self._dims and (path is None or path == self.frame):
            return self._dims
        from .eyes import Eyes
        return Eyes.dims(self, path or self.frame)


# ghosthands key names -> terminal-mac key names (where they differ). On a Mac keyboard the key
# labelled "delete" is backspace; terminal-mac's "delete" is forward delete.
_KEY_ALIASES = {"delete": "backspace", "esc": "escape", "enter": "return", "command": "cmd",
                "option": "alt", "control": "ctrl", "arrow-up": "up", "arrow-down": "down",
                "arrow-left": "left", "arrow-right": "right", "pgdn": "pagedown", "pgup": "pageup"}


class TerminalMacHands(Hands):
    """Hands that act inside the guest. Coordinates arrive as screen fractions (the ghosthands
    contract) and become guest points using the guest's display size."""

    # Pixels of wheel travel per ghosthands scroll notch. ghosthands' sign convention is
    # + = page down; terminal-mac's dy is negative for down.
    SCROLL_PX_PER_NOTCH = 120

    def __init__(self, vm=None, check_ready=True):
        self.vm = vm or TerminalMac()
        s = self.vm.require_ready() if check_ready else self.vm.status()
        self._screen = (int(s.get("width", 1280)), int(s.get("height", 800)))
        self._pos = (self._screen[0] // 2, self._screen[1] // 2)

    def _xy(self, x01, y01):
        w, h = self._screen
        x = int(round(min(1.0, max(0.0, float(x01))) * w))
        y = int(round(min(1.0, max(0.0, float(y01))) * h))
        return min(x, w - 1), min(y, h - 1)

    def move(self, x01, y01):
        self._pos = self._xy(x01, y01)
        self.vm.action("move", *self._pos)
        return "ok"

    def click(self, button="left"):
        args = list(self._pos)
        if button == "right":
            args += ["--button", "right"]
        self.vm.action("click", *args)
        return "ok"

    def double_click(self):
        # One request so the guest sees a real double click (clickState 1 then 2).
        self.vm.action("click", *self._pos, "--double")
        return "ok"

    def type(self, text):
        if text:
            # `--` so text that starts with "-" is never read as an option.
            self.vm.action("type", "--", text)
        return "ok"

    def key(self, keys):
        if isinstance(keys, (list, tuple)):
            parts = [str(k) for k in keys]
        else:
            parts = str(keys).replace(",", "+").split("+")
        parts = [_KEY_ALIASES.get(p.strip().lower(), p.strip().lower()) for p in parts if p.strip()]
        if not parts:
            return "ok"
        self.vm.action("key", "+".join(parts))
        return "ok"

    def scroll(self, amount):
        if isinstance(amount, dict):
            amount = amount.get("amount", 0)
        dy = max(-1000, min(1000, -int(amount) * self.SCROLL_PX_PER_NOTCH))
        if dy:
            self.vm.action("scroll", "--dy", dy, "--x", self._pos[0], "--y", self._pos[1])
        return "ok"

    def navigate(self, url):
        """Open `url` in the guest's front Safari window: cmd+l, clear, type, Return. It assumes
        Safari is the front app in the guest (the planner sees the next frame and can correct).
        The pauses matter: Safari drops a Return that arrives while it is still autocompleting."""
        self.key("cmd+l"); time.sleep(0.6)
        self.key("cmd+a"); self.key("backspace"); time.sleep(0.2)
        self.type(url); time.sleep(1.0)
        self.key("return"); time.sleep(0.3)
        self.key("escape")          # closes a leftover suggestions list; harmless on a loaded page
        return "ok"


def make_terminal_mac(name=None):
    """(eyes, hands) sharing one TerminalMac client."""
    vm = TerminalMac(name)
    return TerminalMacEyes(vm), TerminalMacHands(vm)
