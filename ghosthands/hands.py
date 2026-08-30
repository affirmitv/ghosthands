"""Hands: inject real mouse/keyboard. PicoHands drives a Raspberry Pi Pico flashed as a
USB-HID device (see scripts/firmware) -- the host OS cannot tell it from a human, and it
is DOM-independent (works on any app, not just browsers). DryRunHands logs instead."""
import json, time
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

def make_hands(backend=None):
    backend = backend or Config.hands_backend
    return DryRunHands() if backend == "dryrun" else PicoHands()
