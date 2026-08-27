"""Hands: inject real mouse/keyboard. PicoHands drives a Raspberry Pi Pico flashed as a
USB-HID device (see scripts/firmware) -- the host OS cannot tell it from a human, and it
is DOM-independent (works on any app, not just browsers). DryRunHands logs instead."""
import json, time
try:
    import serial
except ImportError:
    serial = None
from .config import Config

class Hands:
    def move(self, x01, y01): raise NotImplementedError
    def click(self, button="left"): raise NotImplementedError
    def type(self, text): raise NotImplementedError
    def key(self, keys): raise NotImplementedError
    def scroll(self, amount): raise NotImplementedError

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
        # typing needs per-char time or chars drop; clicks/keys are quick
        time.sleep(0.5 + 0.02 * len(str(cmd.get("type", "")))) if cmd.get("type") else time.sleep(0.35)
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
        return self._send({"scroll": amount})

    def ping(self):
        return self._send({"ping": 1})

class DryRunHands(Hands):
    def move(self, x01, y01): print("  [dry] move %.3f,%.3f" % (x01, y01)); return "dry"
    def click(self, button="left"): print("  [dry] click %s" % button); return "dry"
    def type(self, text): print("  [dry] type %r" % text); return "dry"
    def key(self, keys): print("  [dry] key %s" % keys); return "dry"
    def scroll(self, amount): print("  [dry] scroll %s" % amount); return "dry"

def make_hands(backend=None):
    backend = backend or Config.hands_backend
    return DryRunHands() if backend == "dryrun" else PicoHands()
