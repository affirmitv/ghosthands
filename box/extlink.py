"""boxd's side of the vault extension link (box/ext, box/nmhost.py).

boxd listens on a Unix socket only the browser user can reach; the extension's native-messaging
host connects to it. The kernel reports the caller's uid (SO_PEERCRED): anything but the browser
user is dropped. Requests go one at a time: {"id", "op", ...} out, {"id", "result"|"error"} back.

A DevTools connection would answer the same questions, but it marks the browser as automated
(navigator.webdriver) for every page; an extension is how password managers do this.
"""
import json
import os
import socket
import struct
import threading

SOCK = os.environ.get("BOX_EXT_SOCK", "/run/ghostbox-ext/sock")
BROWSER_UID = int(os.environ.get("BOX_BROWSER_UID", "1001"))


class ExtError(RuntimeError):
    pass


class ExtLink:
    def __init__(self, path=SOCK):
        self.path = path
        self._conn = None
        self._buf = b""
        self._id = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        import grp
        os.chown(self.path, -1, grp.getgrnam("browser").gr_gid)   # ghost is in group browser
        os.chmod(self.path, 0o660)
        srv.listen(2)
        while True:
            c, _ = srv.accept()
            pid, uid, gid = struct.unpack("3i", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != BROWSER_UID:
                c.close()
                continue
            with self._lock:
                if self._conn:
                    try:
                        self._conn.close()
                    except OSError:
                        pass
                self._conn, self._buf = c, b""
                self._ready.set()

    def call(self, op, timeout=15, **kw):
        if not self._ready.wait(timeout):
            raise ExtError("the browser's vault extension is not connected")
        with self._lock:
            if not self._conn:
                raise ExtError("the browser's vault extension is not connected")
            self._id += 1
            want = self._id
            try:
                self._conn.settimeout(timeout)
                self._conn.sendall(json.dumps({"id": want, "op": op, **kw}).encode() + b"\n")
                while True:
                    while b"\n" in self._buf:
                        line, self._buf = self._buf.split(b"\n", 1)
                        m = json.loads(line)
                        if m.get("id") != want:
                            continue
                        if "error" in m:
                            raise ExtError(str(m["error"])[:200])
                        return m.get("result")
                    chunk = self._conn.recv(65536)
                    if not chunk:
                        raise ExtError("the extension went away")
                    self._buf += chunk
            except (OSError, ValueError) as e:
                self._conn = None
                self._ready.clear()
                raise ExtError("extension link: %s" % e)
