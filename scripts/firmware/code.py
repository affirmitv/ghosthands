# code.py — AppSpace Pico HID actuator (RP2040 / CircuitPython).
#
# The dumb, reliable end of the "brain drives the real apps" rig:
#   the host machine's brain  --(newline-delimited JSON over usb_cdc.data)-->  THIS Pico
#   THIS Pico        --(real USB HID reports, back into the host machine)---->  the OS/apps
#
# The input arrives as genuine external USB HID: no OS-level synthetic-event
# fingerprint for platforms to detect. The command channel (usb_cdc.data) is a
# separate serial pipe apps can't see. Human realism (eased cursor arcs, keystroke
# jitter, backspace corrections) is synthesized HERE, in firmware, so a single
# high-level command from the brain lands as human-looking input.
#
# CONTRACT: one JSON object per line in; exactly one JSON ack line out per command.
# The brain SHOULD wait for the ack before sending the next command (request/reply).
#
# Commands (see README.md "Command protocol" for the full table):
#   {"move":{"x":0..32767,"y":0..32767}}   absolute cursor move (human arc)
#   {"click":"left"|"right"|"middle"|"double"}
#   {"down":"left"|"right"|"middle"}       press & hold  (for drags)
#   {"up":"left"|"right"|"middle"}         release
#   {"scroll":<int>}                        wheel ticks (+ up / - down)
#   {"type":"some text"}                    typed with human jitter + rare typo+fix
#   {"key":"enter"} | {"key":["cmd","t"]}   a named key or a chord
#   {"ping":true}                           liveness -> ack {"ok":true,"pong":true,...}
#   {"selftest":true[,"text":"..."]}        trace a square + type a string (eyes-on)
# Optional "id" on any command is echoed back in the ack for correlation.
#
# Requires (copy into CIRCUITPY:/lib/):  adafruit_hid/  (keyboard, keycode, layout)
# Requires boot.py from this folder (builds the composite HID + usb_cdc.data).

import json
import math
import random
import sys
import time

import usb_cdc
import usb_hid

from adafruit_hid.keyboard import Keyboard
from adafruit_hid.keycode import Keycode
from adafruit_hid.keyboard_layout_us import KeyboardLayoutUS

# ---------------------------------------------------------------------------------
# Tunables — the "how human does it look" knobs. Safe to adjust live (drag-drop edit).
# ---------------------------------------------------------------------------------
ABS_MAX = 32767                 # logical max of the absolute axes (matches boot.py)
MOVE_MIN_STEPS = 12             # min interpolation samples for a cursor move
MOVE_MAX_STEPS = 90             # max interpolation samples (long full-screen sweep)
MOVE_UNITS_PER_STEP = 620       # ~distance covered per sample (bigger = fewer steps)
MOVE_STEP_SLEEP = (0.006, 0.014)  # per-step delay range (seconds)
MOVE_ARC = 0.14                 # sideways bow of the path, as a fraction of distance
MOVE_OVERSHOOT_UNITS = (90, 480)  # overshoot-then-settle magnitude range
MOVE_OVERSHOOT_MIN_DIST = 2600  # only overshoot when the move is at least this far

CLICK_HOLD = (0.045, 0.11)      # press->release dwell for a click (seconds)
CLICK_GAP = (0.06, 0.13)        # gap between the two clicks of a double-click
KEY_TAP = (0.02, 0.05)          # press->release dwell for a single key/chord

TYPE_DELAY = (0.035, 0.14)      # base inter-keystroke delay
TYPE_WORDPAUSE = (0.12, 0.4)    # occasional longer "thinking" pause
TYPE_WORDPAUSE_PROB = 0.10      # chance of a longer pause after a keystroke
TYPE_TYPO_PROB = 0.035          # chance a keystroke is a wrong-key + backspace + fix

SCROLL_TICK = (1, 3)            # wheel units per emitted report
SCROLL_SLEEP = (0.02, 0.055)    # delay between scroll reports

random.seed(time.monotonic_ns() & 0x7FFFFFFF)

# ---------------------------------------------------------------------------------
# Optional onboard LED — a physical "I'm alive / I just acted" blink. No-op on
# variants without board.LED (e.g. Pico W, where the LED is on the wifi chip).
# ---------------------------------------------------------------------------------
_led = None
try:
    import board
    import digitalio
    _led = digitalio.DigitalInOut(board.LED)
    _led.direction = digitalio.Direction.OUTPUT
except Exception:
    _led = None


def _blink(on):
    if _led is not None:
        try:
            _led.value = bool(on)
        except Exception:
            pass


# ---------------------------------------------------------------------------------
# Serial command channel. boot.py enables usb_cdc.data; fall back to the console only
# if someone flashed code.py without the matching boot.py (degraded, but not bricked).
# ---------------------------------------------------------------------------------
ser = usb_cdc.data if usb_cdc.data is not None else usb_cdc.console
if ser is not None:
    try:
        ser.timeout = 0            # non-blocking reads
    except Exception:
        pass

# ---------------------------------------------------------------------------------
# HID handles: the standard keyboard + our custom absolute mouse (found by usage).
# ---------------------------------------------------------------------------------
# Hardened bring-up: never let a missing keyboard HID brick the whole actuator.
# If Keyboard() can't bind (e.g. endpoint budget dropped the keyboard interface),
# degrade to mouse-only and REPORT it in the ping ack instead of dying silently.
try:
    kbd = Keyboard(usb_hid.devices)
    layout = KeyboardLayoutUS(kbd)
    _setup_err = None
except Exception as _e:
    kbd = None
    layout = None
    _setup_err = repr(_e)


def _find_abs_mouse():
    # Our custom device advertises Generic-Desktop (0x01) / Mouse (0x02).
    for d in usb_hid.devices:
        try:
            if d.usage_page == 0x01 and d.usage == 0x02:
                return d
        except Exception:
            continue
    return None


abs_mouse = _find_abs_mouse()

# Firmware-tracked pointer state. We command ABSOLUTE positions, so we remember the
# last commanded coordinate to interpolate the next move from. Start at screen center.
_cx = ABS_MAX // 2
_cy = ABS_MAX // 2
_buttons = 0  # bitmask: 1=left, 2=right, 4=middle (held across move => drag)

BTN = {"left": 1, "right": 2, "middle": 4}


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _send(x, y, wheel=0):
    """Emit one 6-byte absolute-mouse report at (x,y) with the current button mask."""
    global _cx, _cy
    x = int(_clamp(x, 0, ABS_MAX))
    y = int(_clamp(y, 0, ABS_MAX))
    w = int(_clamp(wheel, -127, 127)) & 0xFF
    if abs_mouse is not None:
        report = bytes((_buttons & 0x07, x & 0xFF, (x >> 8) & 0xFF,
                        y & 0xFF, (y >> 8) & 0xFF, w))
        abs_mouse.send_report(report, 4)
    _cx, _cy = x, y


def _rand(pair):
    return random.uniform(pair[0], pair[1])


# ---------------------------------------------------------------------------------
# Human cursor motion: cubic-bezier arc, eased sampling, overshoot-and-settle.
# ---------------------------------------------------------------------------------
def _bezier(p0, p1, p2, p3, t):
    mt = 1.0 - t
    a = mt * mt * mt
    b = 3 * mt * mt * t
    c = 3 * mt * t * t
    d = t * t * t
    return (a * p0 + b * p1 + c * p2 + d * p3)


def _smoothstep(t):
    # ease-in-out: slow start, quick middle, slow arrival (3t^2 - 2t^3)
    return t * t * (3.0 - 2.0 * t)


def move_to(tx, ty):
    tx = int(_clamp(tx, 0, ABS_MAX))
    ty = int(_clamp(ty, 0, ABS_MAX))
    x0, y0 = _cx, _cy
    dx, dy = tx - x0, ty - y0
    dist = math.sqrt(dx * dx + dy * dy)

    if dist < 1:
        _send(tx, ty)
        return

    # Decide the aim point: overshoot slightly past the target on long moves so the
    # cursor arrives fast then settles back, like a real hand.
    overshoot = dist >= MOVE_OVERSHOOT_MIN_DIST
    if overshoot:
        o = _rand(MOVE_OVERSHOOT_UNITS)
        ax = tx + (dx / dist) * o
        ay = ty + (dy / dist) * o
    else:
        ax, ay = tx, ty

    # Control points bow the path sideways (perpendicular offset at 1/3 and 2/3).
    nx, ny = -dy / dist, dx / dist          # unit normal
    bow = dist * MOVE_ARC
    off1 = random.uniform(-bow, bow)
    off2 = random.uniform(-bow, bow)
    c1x = x0 + dx * 0.33 + nx * off1
    c1y = y0 + dy * 0.33 + ny * off1
    c2x = x0 + dx * 0.66 + nx * off2
    c2y = y0 + dy * 0.66 + ny * off2

    steps = int(dist / MOVE_UNITS_PER_STEP) + MOVE_MIN_STEPS
    steps = int(_clamp(steps, MOVE_MIN_STEPS, MOVE_MAX_STEPS))

    for i in range(1, steps + 1):
        t = _smoothstep(i / steps)
        x = _bezier(x0, c1x, c2x, ax, t)
        y = _bezier(y0, c1y, c2y, ay, t)
        _send(x, y)
        time.sleep(_rand(MOVE_STEP_SLEEP))

    if overshoot:
        # settle back from the overshoot point onto the exact target in 2-3 hops
        for _ in range(random.randint(2, 3)):
            sx = _cx + (tx - _cx) * random.uniform(0.5, 0.8)
            sy = _cy + (ty - _cy) * random.uniform(0.5, 0.8)
            _send(sx, sy)
            time.sleep(_rand((0.01, 0.03)))
    _send(tx, ty)  # land exactly on target


# ---------------------------------------------------------------------------------
# Buttons / clicks / scroll
# ---------------------------------------------------------------------------------
def btn_down(name):
    global _buttons
    _buttons |= BTN.get(name, 1)
    _send(_cx, _cy)


def btn_up(name):
    global _buttons
    _buttons &= ~BTN.get(name, 1)
    _send(_cx, _cy)


def click(name):
    b = BTN.get(name, 1)
    global _buttons
    _buttons |= b
    _send(_cx, _cy)
    time.sleep(_rand(CLICK_HOLD))
    _buttons &= ~b
    _send(_cx, _cy)


def double_click():
    click("left")
    time.sleep(_rand(CLICK_GAP))
    click("left")


def scroll(dy):
    dy = int(dy)
    if dy == 0:
        _send(_cx, _cy, 0)
        return
    sign = 1 if dy > 0 else -1
    remaining = abs(dy)
    while remaining > 0:
        tick = min(remaining, random.randint(SCROLL_TICK[0], SCROLL_TICK[1]))
        _send(_cx, _cy, sign * tick)
        remaining -= tick
        time.sleep(_rand(SCROLL_SLEEP))
    _send(_cx, _cy, 0)


# ---------------------------------------------------------------------------------
# Keyboard: human typing (jitter + occasional typo->backspace->fix) and named keys.
# ---------------------------------------------------------------------------------
_TYPO_NEIGHBORS = "asdfghjklqwertyuiopzxcvbnm"


def type_text(text):
    typed = 0
    for ch in text:
        try:
            # rare human typo: hit a wrong key, notice, backspace, retype the right one
            if ch.isalpha() and random.random() < TYPE_TYPO_PROB:
                wrong = random.choice(_TYPO_NEIGHBORS)
                if wrong != ch.lower():
                    layout.write(wrong)
                    time.sleep(_rand((0.05, 0.13)))
                    kbd.send(Keycode.BACKSPACE)
                    time.sleep(_rand((0.04, 0.1)))
            layout.write(ch)
            typed += 1
        except ValueError:
            # char not representable on a US layout (emoji, smart quotes, …) — skip it
            continue
        time.sleep(_rand(TYPE_DELAY))
        if random.random() < TYPE_WORDPAUSE_PROB:
            time.sleep(_rand(TYPE_WORDPAUSE))
    return typed


# Named keys -> Keycode. Aliases included (cmd/gui, opt/alt, esc/escape, etc.).
_KEYMAP = {
    "enter": Keycode.ENTER, "return": Keycode.ENTER,
    "tab": Keycode.TAB, "space": Keycode.SPACEBAR, "spacebar": Keycode.SPACEBAR,
    "esc": Keycode.ESCAPE, "escape": Keycode.ESCAPE,
    "backspace": Keycode.BACKSPACE, "delete": Keycode.DELETE, "del": Keycode.DELETE,
    "forward_delete": Keycode.DELETE,
    "up": Keycode.UP_ARROW, "down": Keycode.DOWN_ARROW,
    "left": Keycode.LEFT_ARROW, "right": Keycode.RIGHT_ARROW,
    "home": Keycode.HOME, "end": Keycode.END,
    "pageup": Keycode.PAGE_UP, "pagedown": Keycode.PAGE_DOWN,
    "insert": Keycode.INSERT, "caps": Keycode.CAPS_LOCK,
    # modifiers
    "cmd": Keycode.GUI, "command": Keycode.GUI, "gui": Keycode.GUI,
    "win": Keycode.GUI, "meta": Keycode.GUI,
    "ctrl": Keycode.CONTROL, "control": Keycode.CONTROL,
    "alt": Keycode.ALT, "option": Keycode.ALT, "opt": Keycode.ALT,
    "shift": Keycode.SHIFT,
    "minus": Keycode.MINUS, "equals": Keycode.EQUALS, "equal": Keycode.EQUALS,
    "comma": Keycode.COMMA, "period": Keycode.PERIOD, "dot": Keycode.PERIOD,
    "slash": Keycode.FORWARD_SLASH, "backslash": Keycode.BACKSLASH,
    "semicolon": Keycode.SEMICOLON, "quote": Keycode.QUOTE,
}
for _i in range(1, 13):
    _KEYMAP["f%d" % _i] = getattr(Keycode, "F%d" % _i)


def _keycode(name):
    """Resolve a name/char to a Keycode. Single letters/digits map to their key."""
    if not isinstance(name, str) or not name:
        return None
    low = name.lower()
    if low in _KEYMAP:
        return _KEYMAP[low]
    if len(name) == 1:
        ch = name.upper()
        if "A" <= ch <= "Z":
            return getattr(Keycode, ch)
        if "0" <= ch <= "9":
            return getattr(Keycode, "ZERO") if ch == "0" else getattr(
                Keycode, {"1": "ONE", "2": "TWO", "3": "THREE", "4": "FOUR",
                          "5": "FIVE", "6": "SIX", "7": "SEVEN", "8": "EIGHT",
                          "9": "NINE"}[ch])
    return None


def press_key(spec):
    """spec is a single name (str) or a chord (list of names, e.g. ['cmd','t'])."""
    names = spec if isinstance(spec, list) else [spec]
    codes = []
    for n in names:
        kc = _keycode(n)
        if kc is None:
            raise ValueError("unknown key: %r" % (n,))
        codes.append(kc)
    for kc in codes:            # press modifiers+key together
        kbd.press(kc)
    time.sleep(_rand(KEY_TAP))
    kbd.release_all()
    return len(codes)


# ---------------------------------------------------------------------------------
# Self-test: trace a small square then type a string. Purely eyes-on confirmation
# that this Pico is really driving the host machine's cursor + keyboard.
# ---------------------------------------------------------------------------------
def self_test(text):
    # a square roughly in the upper-left quadrant of the logical space
    a, b = 8000, 8000
    c, d = 14000, 14000
    move_to(a, b)
    time.sleep(0.15)
    for (x, y) in ((c, b), (c, d), (a, d), (a, b)):
        move_to(x, y)
        time.sleep(0.12)
    time.sleep(0.2)
    return type_text(text)


# ---------------------------------------------------------------------------------
# Dispatch + ack
# ---------------------------------------------------------------------------------
def _write_ack(obj):
    if ser is None:
        return
    try:
        ser.write((json.dumps(obj) + "\n").encode("utf-8"))
    except Exception:
        pass


def handle(cmd):
    """Execute one parsed command object; return a result dict for the ack."""
    if "ping" in cmd:
        # Diagnostic ping: report what actually enumerated so a headless host can
        # tell keyboard-missing from mouse-missing from all-good in one round-trip.
        try:
            ndev = len(list(usb_hid.devices))
        except Exception:
            ndev = -1
        return {"cmd": "ping", "pong": True,
                "kbd": kbd is not None,
                "abs_mouse": abs_mouse is not None,
                "setup_err": _setup_err,
                "ndev": ndev}

    if "move" in cmd:
        m = cmd["move"]
        move_to(m.get("x", _cx), m.get("y", _cy))
        return {"cmd": "move", "x": _cx, "y": _cy}

    if "click" in cmd:
        which = cmd["click"]
        if which == "double":
            double_click()
        else:
            click(which)
        return {"cmd": "click", "which": which, "x": _cx, "y": _cy}

    if "down" in cmd:
        btn_down(cmd["down"])
        return {"cmd": "down", "which": cmd["down"]}

    if "up" in cmd:
        btn_up(cmd["up"])
        return {"cmd": "up", "which": cmd["up"]}

    if "scroll" in cmd:
        scroll(cmd["scroll"])
        return {"cmd": "scroll", "dy": int(cmd["scroll"])}

    if "type" in cmd:
        n = type_text(str(cmd["type"]))
        return {"cmd": "type", "chars": n}

    if "key" in cmd:
        n = press_key(cmd["key"])
        return {"cmd": "key", "keys": n}

    if "selftest" in cmd:
        n = self_test(cmd.get("text", "AppSpace Pico HID OK 01234"))
        return {"cmd": "selftest", "chars": n}

    raise ValueError("no known action in command")


def process_line(line):
    cid = None
    try:
        cmd = json.loads(line)
        if isinstance(cmd, dict):
            cid = cmd.get("id")
        else:
            raise ValueError("command must be a JSON object")
        _blink(True)
        result = handle(cmd)
        _blink(False)
        result["ok"] = True
        if cid is not None:
            result["id"] = cid
        _write_ack(result)
    except Exception as e:
        _blink(False)
        _write_ack({"ok": False, "err": str(e), "id": cid})


# ---------------------------------------------------------------------------------
# Main loop: accumulate bytes, split on newline, process each complete command line.
# Never crashes the loop — a bad command acks an error and we keep serving.
# ---------------------------------------------------------------------------------
_buf = bytearray()


def _poll_line():
    global _buf
    if ser is None:
        return None
    try:
        n = ser.in_waiting
    except Exception:
        # console fallback lacks in_waiting; use blocking-ish readline via stdin
        s = sys.stdin.readline()
        return s.strip() if s else None
    if n:
        _buf += ser.read(n)
    nl = _buf.find(b"\n")
    if nl < 0:
        return None
    raw = _buf[:nl]
    # CircuitPython bytearray does NOT support slice deletion (del _buf[:n] raises
    # TypeError) — reassign the tail instead. This is THE bug that silently killed
    # the actuator on the first command arriving.
    _buf = _buf[nl + 1:]
    return bytes(raw).decode("utf-8", "ignore").strip()


# two-blink boot heartbeat blink heartbeat so you can see the firmware started
for _ in range(2):
    _blink(True)
    time.sleep(0.08)
    _blink(False)
    time.sleep(0.08)

while True:
    line = _poll_line()
    if line:
        process_line(line)
    else:
        time.sleep(0.004)
