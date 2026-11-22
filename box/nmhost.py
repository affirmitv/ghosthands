#!/usr/bin/env python3
"""Native-messaging host for the ghostbox vault extension (box/ext). Chrome starts it, as the
browser user, when the extension connects; it relays between the extension (stdin/stdout,
4-byte length + JSON) and boxd's socket (/run/ghostbox-ext/sock, one JSON object per line).
It holds nothing itself and exits when either side goes away."""
import json
import os
import select
import socket
import struct
import sys

SOCK = os.environ.get("BOX_EXT_SOCK", "/run/ghostbox-ext/sock")


def read_ext(stream):
    head = stream.read(4)
    if len(head) < 4:
        return None
    n = struct.unpack("=I", head)[0]
    return json.loads(stream.read(n))


def write_ext(stream, obj):
    data = json.dumps(obj).encode()
    stream.write(struct.pack("=I", len(data)) + data)
    stream.flush()


def main():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
    except OSError:
        return 1
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    buf = b""
    while True:
        ready, _, _ = select.select([stdin, s], [], [])
        if stdin in ready:
            msg = read_ext(stdin)
            if msg is None:
                return 0
            s.sendall(json.dumps(msg).encode() + b"\n")
        if s in ready:
            chunk = s.recv(65536)
            if not chunk:
                return 0
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    write_ext(stdout, json.loads(line))


if __name__ == "__main__":
    sys.exit(main())
