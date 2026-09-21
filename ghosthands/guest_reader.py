"""ghosthands.guest_reader — read the Tart guest's Safari tab as an element table.

The Jev planner needs a structured DOM element table, but over VNC the host
only gets pixels. This reader runs the same SNAPSHOT_JS as dom_reader inside
the guest (over SSH + osascript) and builds the same Screen objects, so
JevPlanner works unchanged against the VM guest.

Requires in the guest: Safari > Develop > Allow JavaScript from Apple Events
(``defaults write com.apple.Safari AllowJavaScriptFromAppleEvents -bool true``).

Implementation note: the AppleScript is staged as files in the guest and run
with ``osascript /path/to.scpt``. Passing the script via ``osascript -e``
through ssh breaks because the guest's zsh interprets the parentheses.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

from .dom_reader import SNAPSHOT_JS, Screen, SafariReaderError

_GUEST_JS_PATH = "/tmp/gh_snapshot.js"
_GUEST_DO_JS_SCPT = "/tmp/gh_do_js.scpt"
_GUEST_NAV_SCPT = "/tmp/gh_nav.scpt"
_js_present = False


def _guest() -> str:
    user = os.environ.get("GH_GUEST_SSH_USER", "admin")
    host = os.environ.get("GH_GUEST_SSH_HOST", "192.168.64.3")
    return "%s@%s" % (user, host)


def _ssh(*args: str, timeout: float = 30.0) -> str:
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=10", _guest(), *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise SafariReaderError("guest ssh failed: %s" % (p.stderr.strip() or p.stdout.strip()))
    return p.stdout.strip()


def _scp_to_guest(local: str, remote: str) -> None:
    p = subprocess.run(["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                        "-o", "ConnectTimeout=10", local, "%s:%s" % (_guest(), remote)],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise SafariReaderError("guest scp failed: %s" % (p.stderr.strip() or p.stdout.strip()))


def _ensure_staged() -> None:
    """Stage the snapshot JS + driver AppleScript in the guest once per process."""
    global _js_present
    if _js_present:
        return
    with open("/tmp/gh_snapshot.js", "w") as f:
        f.write(SNAPSHOT_JS)
    with open("/tmp/gh_do_js.scpt", "w") as f:
        f.write("tell application \"Safari\" to do JavaScript "
                "(read POSIX file \"%s\") in current tab of front window\n" % _GUEST_JS_PATH)
    _scp_to_guest("/tmp/gh_snapshot.js", _GUEST_JS_PATH)
    _scp_to_guest("/tmp/gh_do_js.scpt", _GUEST_DO_JS_SCPT)
    _js_present = True


class GuestSafariReader:
    """Reads the guest's front Safari tab through SSH + AppleScript JS bridge."""

    def __init__(self) -> None:
        _ensure_staged()

    def _do_js(self, timeout: float = 25.0) -> str:
        out = _ssh("osascript", _GUEST_DO_JS_SCPT, timeout=timeout)
        if "execution error" in out or "not allowed" in out.lower():
            raise SafariReaderError(out)
        return out

    def snapshot(self) -> Screen:
        try:
            data = json.loads(self._do_js())
        except (json.JSONDecodeError, TypeError):
            time.sleep(0.4)
            try:
                data = json.loads(self._do_js())
            except (json.JSONDecodeError, TypeError) as e:
                raise SafariReaderError("guest Safari returned non-JSON: %s" % e)
        vp = data.get("viewport") or {}
        if vp.get("sw") and vp.get("sh"):
            size = (int(vp["sw"]), int(vp["sh"]))
        else:
            size = (1920, 1080)  # Tart guest framebuffer
        return Screen.from_json(data, size)

    def navigate(self, url: str) -> None:
        lit = "\"" + url.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
        with open("/tmp/gh_nav.scpt", "w") as f:
            f.write("tell application \"Safari\" to set URL of current tab of front window to "
                    + lit + "\n")
        _scp_to_guest("/tmp/gh_nav.scpt", _GUEST_NAV_SCPT)
        _ssh("osascript", _GUEST_NAV_SCPT, timeout=20.0)
