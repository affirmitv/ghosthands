"""VNC (RFB 3.8) backend: drive a Tart macOS VM with zero host-screen interaction.

Pure-python, dependency-light (stdlib only; Pillow is optional and used
only by frame_image()).
Selected with GH_HANDS=vnc; the guest is addressed with GH_VNC_HOST,
GH_VNC_PORT (default 5900) and GH_VNC_PASSWORD_FILE
(default ~/.config/ghosthands/tart-vnc.env, holding a VNC_PASSWORD=... line).

Guest setup notes (see docs/tart-vm.md):
- macOS Screen Sharing enabled with the legacy VNC password (RFB security type 2).
- Guest scroll direction set to traditional (not "natural") so positive scroll
  amounts page down, matching the PicoHands/cliclick convention.

Coordinate model: every Hands method takes fractions of the guest framebuffer
(0..1), exactly like the other backends; VNCHands maps them to guest pixels.
"""
import re, socket, struct, time, zlib
from .config import Config
from ._des import vnc_password_response

ENC_RAW = 0


def _png_chunk(typ, data):
    body = typ + data
    return (struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

def load_vnc_password(path=None):
    if Config.vnc_password:
        return Config.vnc_password
    path = path or Config.vnc_password_file
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r"\s*(?:export\s+)?VNC_PASSWORD\s*=\s*(.+)", line)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    raise RuntimeError("no VNC_PASSWORD found in %s" % path)


def open_session(host=None, port=None, password=None, password_file=None):
    """Open ONE persistent RFB connection to share between VNCHands and VNCEyes.

    This matters for stability: tart's _VZVNCServer has crashed (SIGTRAP) under
    VNC connection churn, so a run should connect once and reuse the session
    for every capture and input event instead of connecting per operation.
    """
    host = host or Config.vnc_host
    port = port or Config.vnc_port
    if not host:
        raise RuntimeError("GH_VNC_HOST is not set (run bin/tart-vm-up first)")
    pw = password if password is not None else load_vnc_password(password_file)
    rfb = RFBClient(host, port, pw)
    rfb.connect()
    return rfb

class RFBError(Exception):
    pass

class RFBClient:
    """Minimal RFB 3.8 client: VNC-Auth, 32-bit little-endian XRGB pixels, Raw rects,
    pointer events, key events. Enough to see the guest screen and drive it."""
    def __init__(self, host, port, password=None, timeout=10):
        self.host, self.port = host, port
        self.password = password
        self.timeout = timeout
        self.sock = None
        self.width = self.height = 0
        self.name = ""

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RFBError("connection closed by server")
            buf += chunk
        return buf

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        ver = self._recv(12)
        if not ver.startswith(b"RFB 003."):
            raise RFBError("unexpected server version %r" % ver)
        self.sock.sendall(b"RFB 003.008\n")
        ntypes = self._recv(1)[0]
        if ntypes == 0:
            ln = struct.unpack(">I", self._recv(4))[0]
            raise RFBError("server refused: %s" % self._recv(ln).decode("latin1", "replace"))
        types = list(self._recv(ntypes))
        if 2 in types and self.password:
            chosen = 2
        elif 1 in types:
            chosen = 1
        elif 2 in types:
            raise RFBError("server requires a VNC password but none is configured")
        else:
            raise RFBError("no supported security type, server offers %r" % types)
        self.sock.sendall(bytes([chosen]))
        if chosen == 2:
            challenge = self._recv(16)
            self.sock.sendall(vnc_password_response(self.password, challenge))
            result = struct.unpack(">I", self._recv(4))[0]
            if result != 0:
                raise RFBError("VNC password rejected by server")
        self.sock.sendall(b"\x01")  # ClientInit: shared session (matches vncdotool).
        si = self._recv(24)
        self.width, self.height = struct.unpack(">HH", si[:4])
        if self.width == 0 or self.height == 0:
            raise RFBError("server reported zero-size framebuffer")
        nlen = struct.unpack(">I", si[20:24])[0]
        if nlen:
            self.name = self._recv(nlen).decode("latin1", "replace")
        # NOTE: we deliberately do NOT send SetPixelFormat. Apple's VNC servers
        # (both the guest ARD/Screen Sharing server and the
        # Virtualization.framework server tart exposes) drop the connection
        # when the client sends SetPixelFormat; they serve their native
        # 32-bit little-endian XRGB and standard clients just accept it.
        # Offer Raw plus the pseudo-encodings Apple's Virtualization.framework
        # VNC backend (tart --vnc-experimental) requires. That backend
        # deliberately TRAPS (killing the whole VM) when the client's
        # SetEncodings omits a pseudo-encoding it expects
        # ("It is unclear if we can support clients that don't support this
        # pseudo encoding"). This list mirrors vncdotool's working default:
        # Raw(0), DesktopSize(-223), LastRect(-224), QEMUExtendedKey(-258).
        # We never send extended key events; offering -258 just keeps the
        # backend alive. Framebuffer rects still arrive Raw.
        enc = [ENC_RAW, -223, -224, -258]
        self.sock.sendall(struct.pack(">BBH", 2, 0, len(enc))
                          + b"".join(struct.pack(">i", e) for e in enc))
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def frame_bytes(self):
        """Full-screen update; returns (width, height, BGRX bytes)."""
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
        mtype = self._recv(1)[0]
        if mtype != 0:
            raise RFBError("expected FramebufferUpdate, got message type %d" % mtype)
        self._recv(1)  # padding
        nrects = struct.unpack(">H", self._recv(2))[0]
        buf = bytearray(self.width * self.height * 4)
        for _ in range(nrects):
            x, y, w, h, enc = struct.unpack(">HHHHi", self._recv(12))
            if enc == -223:  # DesktopSize pseudo-rect: new w/h, no pixel data
                self.width, self.height = w, h
                buf = bytearray(self.width * self.height * 4)
                continue
            if enc == -224:  # LastRect pseudo-rect: no pixel data
                continue
            if enc != ENC_RAW:
                raise RFBError("server sent non-Raw encoding %d" % enc)
            nbytes = w * h * 4
            data = self._recv(nbytes)
            if w == self.width:
                buf[y * self.width * 4:(y + h) * self.width * 4] = data
            else:
                for row in range(h):
                    o = ((y + row) * self.width + x) * 4
                    buf[o:o + w * 4] = data[row * w * 4:(row + 1) * w * 4]
        return self.width, self.height, bytes(buf)

    def frame_image(self):
        from PIL import Image
        w, h, data = self.frame_bytes()
        return Image.frombytes("RGB", (w, h), data, "raw", "BGRX")

    def frame_png_bytes(self):
        """Current framebuffer as PNG bytes. Stdlib only (no Pillow)."""
        w, h, bgrx = self.frame_bytes()
        rgb = bytearray(w * h * 3)
        rgb[0::3] = bgrx[2::4]  # R
        rgb[1::3] = bgrx[1::4]  # G
        rgb[2::3] = bgrx[0::4]  # B
        stride = w * 3
        raw = bytearray(h * (stride + 1))
        for y in range(h):
            o = y * (stride + 1)
            raw[o] = 0  # filter type: none
            raw[o + 1:o + 1 + stride] = rgb[y * stride:(y + 1) * stride]
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n"
                + _png_chunk(b"IHDR", ihdr)
                + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                + _png_chunk(b"IEND", b""))

    def save_screenshot(self, path):
        """Save the current framebuffer as a PNG file (no Pillow needed)."""
        with open(path, "wb") as f:
            f.write(self.frame_png_bytes())
        return path

    def pointer(self, x, y, mask):
        x = max(0, min(self.width - 1, int(x)))
        y = max(0, min(self.height - 1, int(y)))
        self.sock.sendall(struct.pack(">BBHH", 5, mask & 0xFF, x, y))

    def key(self, keysym, down=True):
        self.sock.sendall(struct.pack(">BBxxI", 4, 1 if down else 0, keysym & 0xFFFFFFFF))

    def key_tap(self, keysym):
        self.key(keysym, True); self.key(keysym, False)

# Keysyms (X11 names; macOS Screen Sharing maps Meta_L to the Command key).
KS_SHIFT = 0xFFE1
KS_CTRL = 0xFFE3
KS_META = 0xFFE7   # Command on macOS
KS_ALT = 0xFFE9
_KS_NAMES = {
    "return": 0xFF0D, "enter": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B, "esc": 0xFF1B,
    "backspace": 0xFF08, "delete": 0xFFFF, "space": 0x20,
    "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
}
_KS_MODS = {"cmd": KS_META, "command": KS_META, "meta": KS_META,
            "shift": KS_SHIFT, "alt": KS_ALT, "option": KS_ALT,
            "ctrl": KS_CTRL, "control": KS_CTRL}
# chars that need Shift held -> the unshifted key they live on
_SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
            "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
            "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`"}

class VNCHands:
    """Hands implementation over RFB. Same fractional-coordinate contract as the
    other backends; scroll: positive amount = page down (guest must use
    traditional scroll direction, not macOS "natural")."""
    # The Tart VNC server maps Meta_L to Option instead of Command, so
    # Command-modified shortcuts (Cmd+A select-all, Cmd+L focus URL bar) do not
    # work through these hands. Callers must use modifier-free alternatives.
    no_cmd_modifiers = True

    def __init__(self, host=None, port=None, password=None, password_file=None, rfb=None):
        self._owns_rfb = rfb is None
        if rfb is not None:
            self._rfb = rfb
        else:
            self.host = host or Config.vnc_host
            self.port = port or Config.vnc_port
            if not self.host:
                raise RuntimeError("GH_VNC_HOST is not set")
            # password= overrides the file; needed when the VNC password is
            # dynamic (e.g. tart --vnc-experimental generates one per boot).
            pw = password if password is not None else load_vnc_password(password_file)
            self._rfb = RFBClient(self.host, self.port, pw)
            self._rfb.connect()
        self._pos = (0.5, 0.5)

    def close(self):
        if self._owns_rfb:
            self._rfb.close()

    @property
    def dims(self):
        return self._rfb.width, self._rfb.height

    def _xy(self):
        w, h = self.dims
        return (min(1.0, max(0.0, self._pos[0])) * (w - 1),
                min(1.0, max(0.0, self._pos[1])) * (h - 1))

    def move(self, x01, y01):
        self._pos = (x01, y01)
        x, y = self._xy()
        self._rfb.pointer(x, y, 0)
        return "ok"

    def click(self, button="left"):
        x, y = self._xy()
        mask = 4 if button == "right" else 1
        self._rfb.pointer(x, y, mask); time.sleep(0.07)
        self._rfb.pointer(x, y, 0)
        return "ok"

    def _type_char(self, ch):
        if ch == "\n":
            self._rfb.key_tap(_KS_NAMES["return"]); return
        if ch == "\t":
            self._rfb.key_tap(_KS_NAMES["tab"]); return
        base, shift = ch, False
        if "A" <= ch <= "Z":
            base, shift = ch.lower(), True
        elif ch in _SHIFTED:
            base, shift = _SHIFTED[ch], True
        if not 0x20 <= ord(base) <= 0x7E:
            return  # outside latin-1 printable: skip rather than mistype
        if shift:
            self._rfb.key(KS_SHIFT, True)
        self._rfb.key_tap(ord(base))
        if shift:
            self._rfb.key(KS_SHIFT, False)

    def type(self, text):
        for ch in text:
            self._type_char(ch)
            time.sleep(0.008)
        time.sleep(0.15)
        return "ok"

    def key(self, keys):
        """'cmd+l', 'return', 'cmd+shift+t', ... (same grammar as CliclickHands)."""
        parts = [p.strip().lower() for p in str(keys).replace("+", ",").split(",") if p.strip()]
        mods = [_KS_MODS[p] for p in parts if p in _KS_MODS]
        main = [p for p in parts if p not in _KS_MODS][-1:]
        if not main:
            return "ok"
        name = main[0]
        keysym = _KS_NAMES.get(name, ord(name[:1]) if len(name) == 1 else None)
        if keysym is None:
            raise ValueError("unknown key %r" % name)
        for m in mods:
            self._rfb.key(m, True)
        time.sleep(0.03)
        self._rfb.key_tap(keysym)
        for m in reversed(mods):
            self._rfb.key(m, False)
        return "ok"

    def scroll(self, amount):
        # int notches; positive = page DOWN (matches PicoHands/cliclick convention).
        amt = amount.get("amount", 0) if isinstance(amount, dict) else amount
        n = min(max(abs(int(amt)), 0), 30)
        if n == 0:
            return "ok"
        x, y = self._xy()
        btn = (1 << 3) if int(amt) < 0 else (1 << 4)
        for _ in range(n):
            self._rfb.pointer(x, y, btn); time.sleep(0.025)
            self._rfb.pointer(x, y, 0); time.sleep(0.025)
        return "ok"

    def scroll_smooth(self, amount, steps_per_notch=None):
        return self.scroll(amount)

    def ping(self):
        return "vnc %dx%d %s" % (self._rfb.width, self._rfb.height, self._rfb.name)

class VNCEyes:
    """Eyes implementation over RFB: capture() pulls a guest framebuffer update
    and saves it as JPEG (same contract as the screenfeed-based Eyes)."""
    def __init__(self, host=None, port=None, password=None, password_file=None, frame=None, rfb=None):
        self._rfb = rfb  # shared persistent session; None -> connect per grab
        self.host = host or Config.vnc_host
        self.port = port or Config.vnc_port
        self.password = password  # overrides password_file when set
        self.password_file = password_file
        if self._rfb is None and not self.host:
            raise RuntimeError("GH_VNC_HOST is not set")
        self.frame = frame or Config.frame
        self._dims = None

    def _grab(self):
        if self._rfb is not None:
            img = self._rfb.frame_image()
            self._dims = (self._rfb.width, self._rfb.height)
            return img
        pw = self.password if self.password is not None else load_vnc_password(self.password_file)
        rfb = RFBClient(self.host, self.port, pw)
        try:
            rfb.connect()
            img = rfb.frame_image()
            self._dims = (rfb.width, rfb.height)
        finally:
            rfb.close()
        return img

    def capture(self, timeout=20.0):
        img = self._grab()
        img.save(self.frame, "JPEG", quality=82)
        return self.frame

    def dims(self, path=None):
        if self._dims is None:
            self._grab()
        return self._dims

def vnc_navigate(url, host=None, port=None, password=None, password_file=None, rfb=None):
    """Open Safari in the guest and go to url -- the VNC replacement for the
    host-side osascript safari_navigate. Spotlight-launches Safari so it works
    no matter which app is frontmost."""
    h = VNCHands(host, port, password=password, password_file=password_file, rfb=rfb)
    try:
        h.key("cmd+space"); time.sleep(0.7)
        h.type("Safari"); time.sleep(0.5)
        h.key("return"); time.sleep(2.0)
        h.key("cmd+l"); time.sleep(0.5)
        h.key("cmd+a"); time.sleep(0.2)
        h.type(url); time.sleep(0.3)
        h.key("return"); time.sleep(2.5)
    finally:
        h.close()
    return "ok"
