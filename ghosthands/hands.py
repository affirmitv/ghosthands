"""Hands: inject real mouse/keyboard. PicoHands drives a Raspberry Pi Pico flashed as a
USB-HID device (see scripts/firmware) -- the host OS cannot tell it from a human, and it
is DOM-independent (works on any app, not just browsers). DryRunHands logs instead."""
import json, os, time
try:
    import serial
except ImportError:
    serial = None
from .config import Config

# Keep in sync with firmware SCROLL_STEPS_PER_NOTCH (scripts/firmware/code.py). Used ONLY to size
# the ack read-window for a scroll; over-estimating is safe (just waits a touch longer for the ack).
_SCROLL_SPN_DEFAULT = 2

class Hands:
    def move(self, x01, y01): raise NotImplementedError
    def click(self, button="left"): raise NotImplementedError
    def type(self, text): raise NotImplementedError
    def key(self, keys): raise NotImplementedError
    def scroll(self, amount): raise NotImplementedError
    def scroll_smooth(self, amount, steps_per_notch=None):
        # default: build the object form on top of scroll()
        cmd = {"amount": amount}
        if steps_per_notch is not None:
            cmd["steps_per_notch"] = steps_per_notch
        return self.scroll(cmd)

class PicoHands(Hands):
    """One JSON command per line over USB CDC serial; one ack per command."""
    def __init__(self, port=None):
        if serial is None:
            raise RuntimeError("pyserial not installed (pip install pyserial)")
        self.port = port or Config.pico_port

    def _send(self, cmd):
        p = serial.Serial(self.port, 115200, timeout=0.4)
        p.dtr = True; time.sleep(0.12); p.reset_input_buffer()
        p.write((json.dumps(cmd) + "\n").encode()); p.flush()
        # The firmware writes its ack only AFTER the action completes, so the read window must cover
        # the action's duration. Typing and smooth-scrolling both scale with length; a fixed 0.35s
        # misses a long scroll's ack -- which then gets read as the NEXT command's ack. Estimate it.
        if cmd.get("type"):
            wait = 0.5 + 0.02 * len(str(cmd.get("type", "")))
        elif "scroll" in cmd:
            s = cmd["scroll"]
            if isinstance(s, dict):
                amt = abs(int(s.get("amount", 0)))
                spn = int(s.get("steps_per_notch", _SCROLL_SPN_DEFAULT))
                pace = float(s.get("pace", 1.0))
            else:
                amt = abs(int(s)); spn = _SCROLL_SPN_DEFAULT; pace = 1.0
            # ~amt*spn eased unit-reports (~33ms avg each), scaled by pace, + margin
            wait = min(8.0, 0.4 + amt * spn * 0.04 * max(1.0, pace))
        else:
            wait = 0.35
        time.sleep(wait)
        ack = p.read(500).decode("utf-8", "ignore"); p.close()
        for ln in ack.replace("\r", "\n").split("\n"):
            if ln.strip().startswith("{"):
                return ln.strip()
        return ack.strip()

    def move(self, x01, y01):
        px = round(max(0.0, min(1.0, x01)) * 32767)
        py = round(max(0.0, min(1.0, y01)) * 32767)
        return self._send({"move": {"x": px, "y": py}})

    def click(self, button="left"):
        return self._send({"click": button})

    def type(self, text):
        # chunk long strings (<=18 chars) -- the HID stack drops chars on long bursts
        last = ""
        for i in range(0, len(text), 18):
            last = self._send({"type": text[i:i+18]}); time.sleep(0.15)
        return last

    def key(self, keys):
        if isinstance(keys, str):
            keys = keys.replace("+", ",")
            keys = keys.split(",") if "," in keys else keys
        return self._send({"key": keys})

    def scroll(self, amount):
        # int (notches; sign = wheel direction, page direction depends on the host's scroll-direction
        # setting -- on tested macOS + = page DOWN) or the object form
        # {"amount":..,"steps_per_notch":..,"smooth":..} passed through unchanged; firmware resolves both.
        return self._send({"scroll": amount})

    def scroll_smooth(self, amount, steps_per_notch=None):
        cmd = {"amount": amount}
        if steps_per_notch is not None:
            cmd["steps_per_notch"] = steps_per_notch
        return self.scroll(cmd)

    def ping(self):
        return self._send({"ping": 1})

class DryRunHands(Hands):
    def move(self, x01, y01): print("  [dry] move %.3f,%.3f" % (x01, y01)); return "dry"
    def click(self, button="left"): print("  [dry] click %s" % button); return "dry"
    def type(self, text): print("  [dry] type %r" % text); return "dry"
    def key(self, keys): print("  [dry] key %s" % keys); return "dry"
    def scroll(self, amount):
        resolved = amount if isinstance(amount, dict) else {"amount": amount}
        print("  [dry] scroll %s" % resolved); return "dry"

class CliclickHands(Hands):
    """Development hands for a Mac WITHOUT a Pico: real pointer and keyboard events through
    `cliclick` (CGEvent based; `brew install cliclick`). Synthetic as far as the OS is concerned,
    so it is for testing the loop on your own machine, not for the field. Coordinates are
    fractions of the screen Safari is on; the screen size is read from the front Safari tab."""
    def __init__(self, binary=None):
        import shutil, subprocess
        self._bin = binary or shutil.which("cliclick") or "/opt/homebrew/bin/cliclick"
        if not os.path.exists(self._bin):
            raise RuntimeError("cliclick not found; brew install cliclick")
        self._sub = subprocess
        self._pos = (0.5, 0.5)
        r = subprocess.run(["osascript", "-e",
            'tell application "Safari" to do JavaScript "String(screen.width) + String.fromCharCode(32) + String(screen.height)" in current tab of front window'],
            capture_output=True, text=True, timeout=20)
        parts = r.stdout.strip().split()
        if r.returncode != 0 or len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            raise RuntimeError("cannot read the screen size from Safari (%s); enable Develop > "
                               "Allow JavaScript from Apple Events" % (r.stderr.strip() or r.stdout.strip()))
        self._screen = (int(parts[0]), int(parts[1]))
    def _cc(self, *args):
        self._sub.run([self._bin, *args], check=True, capture_output=True, text=True, timeout=20)
    def _xy(self):
        return int(self._pos[0] * self._screen[0]), int(self._pos[1] * self._screen[1])
    def move(self, x01, y01):
        self._pos = (min(1.0, max(0.0, x01)), min(1.0, max(0.0, y01)))
        x, y = self._xy(); self._cc("m:%d,%d" % (x, y)); return "ok"
    def click(self, button="left"):
        x, y = self._xy(); self._cc(("rc:%d,%d" if button == "right" else "c:%d,%d") % (x, y)); return "ok"
    def type(self, text):
        # cliclick's t: takes the text as one argv element; nothing is interpolated into a script.
        if text:
            self._cc("t:" + text)
        return "ok"
    def key(self, keys):
        parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
        mods = {"cmd": "cmd", "command": "cmd", "shift": "shift", "alt": "alt", "option": "alt", "ctrl": "ctrl"}
        names = {"return": "return", "enter": "return", "tab": "tab", "escape": "esc", "esc": "esc",
                 "space": "space", "delete": "delete", "backspace": "delete", "up": "arrow-up",
                 "down": "arrow-down", "left": "arrow-left", "right": "arrow-right"}
        held = [mods[p] for p in parts if p in mods]
        main = [p for p in parts if p not in mods][-1:]
        if not main:
            return "ok"
        seq = []
        if held:
            seq.append("kd:" + ",".join(held))
        seq.append(("kp:" + names[main[0]]) if main[0] in names else ("t:" + main[0][:1]))
        if held:
            seq.append("ku:" + ",".join(held))
        self._cc(*seq); return "ok"
    def scroll(self, amount):
        # cliclick has no wheel event; for dev runs let Safari scroll its own page.
        self._sub.run(["osascript", "-e",
            'tell application "Safari" to do JavaScript "window.scrollBy(0, %d)" in current tab of front window'
            % (int(amount) * 120)], check=False, capture_output=True, timeout=20)
        return "ok"

def make_hands(backend=None):
    backend = backend or Config.hands_backend
    if backend == "dryrun":
        return DryRunHands()
    if backend == "cliclick":
        return CliclickHands()
    return PicoHands()
