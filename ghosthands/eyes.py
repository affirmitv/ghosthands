"""Eyes: get a fresh screenshot for the agent. Works with scripts/screenfeed.sh,
which must run inside a GUI Terminal (macOS TCC blocks screen capture from SSH)."""
import os, time, re, subprocess
from .config import Config

class Eyes:
    def __init__(self, frame=None, trigger=None):
        self.frame = frame or Config.frame
        self.trigger = trigger or Config.trigger

    def capture(self, timeout=12.0):
        try:
            os.remove(self.frame)
        except OSError:
            pass
        open(self.trigger, "w").close()          # ignored by continuous-mode feeds
        t0 = time.time()
        while time.time() - t0 < timeout:
            if os.path.exists(self.frame) and os.path.getsize(self.frame) > 2000:
                time.sleep(0.03)                 # let the atomic mv settle
                return self.frame
            time.sleep(0.02)
        raise RuntimeError("no fresh frame -- is scripts/screenfeed.sh running in a GUI Terminal?")

    def dims(self, path=None):
        p = path or self.frame
        out = subprocess.check_output(["sips", "-g", "pixelWidth", "-g", "pixelHeight", p]).decode()
        w = int(re.search(r'pixelWidth: (\d+)', out).group(1))
        h = int(re.search(r'pixelHeight: (\d+)', out).group(1))
        return w, h
