# Tart VM runbook — headless macOS automation backend

Ghosthands drives an isolated macOS VM (Tart) with **zero host-screen
interaction**. The VM runs headless under a launchd agent; Ghosthands talks
to it over VNC (RFB 3.8). This doc covers start/stop, the launchd service,
crash behavior, dynamic credentials, and the one-connection rule.

## Quick start

```bash
# Make sure the VM is up and the VNC endpoint is live. Writes the current
# endpoint to ~/.config/ghosthands/tart-vnc-current.env (mode 0600).
~/Development/ghosthands/bin/tart-vm-up

# Then run ghosthands with the VNC backend (reads that file automatically):
GH_HANDS=vnc python3 run.py --goal-file <task> --run-dir <dir>
```

`bin/tart-vm-up` never prints the password. It waits for the VM, parses the
dynamic VNC URL from the launchd log, and verifies the endpoint with one
complete RFB handshake (banner → auth → ServerInit).

## Architecture

- **VM**: `mac-gui` (Tart, Apple Virtualization.framework). 50 GB disk,
  guest user `admin` with passwordless sudo, auto-login enabled.
- **Host**: Eric's Mac Studio. Tart installed at `~/Applications/tart.app`
  (+ `/opt/homebrew/bin/tart` symlink), version 2.37.0.
- **launchd agent**: `~/Library/LaunchAgents/com.brkd.tart.mac-gui.plist`
  (label `com.brkd.tart.mac-gui`, `RunAtLoad`, `KeepAlive`,
  `ThrottleInterval=30`). Launches:
  `tart run mac-gui --vnc-experimental --no-graphics --no-audio`
- **VNC**: Tart's experimental `_VZVNCServer` on 127.0.0.1 with an ephemeral
  port and a generated password (both change on every tart restart).
- **Log**: `~/Library/Logs/tart-mac-gui.log` — the VNC URL
  (`vnc://:<password>@127.0.0.1:<port>`) is printed here per boot.

## Start / stop / restart

```bash
launchctl load    ~/Library/LaunchAgents/com.brkd.tart.mac-gui.plist  # start
launchctl unload  ~/Library/LaunchAgents/com.brkd.tart.mac-gui.plist  # stop
# restart = unload, then load. Tart reboots the VM; the VNC port/password change.
```

After any restart, run `bin/tart-vm-up` again to refresh
`~/.config/ghosthands/tart-vnc-current.env`.

Guest SSH (for setup, not automation): `ssh admin@192.168.64.3`
(key auth; see `~/.config/ghosthands/guest-login.env`, mode 0600).
The guest reaches this host at `192.168.64.1` (Tart gateway).

## Dynamic credentials

The experimental VNC password and port are generated fresh on every tart
start. `bin/tart-vm-up` records the current values in
`~/.config/ghosthands/tart-vnc-current.env`:

```
VNC_HOST=127.0.0.1
VNC_PORT=<ephemeral>
VNC_PASSWORD=<generated>   # never printed, never committed
VNC_TART_PID=<pid>         # which tart process these belong to
```

`ghosthands/config.py` reads this file automatically; `GH_VNC_HOST`,
`GH_VNC_PORT`, `GH_VNC_PASSWORD`, `GH_VNC_PASSWORD_FILE` env vars override it.

## The one-connection rule

**Open one RFB connection per Ghosthands run and share it between hands,
eyes, and navigation. Do not connect/disconnect per screenshot or action.**

Why: `_VZVNCServer` has crashed with `EXC_BREAKPOINT`/`SIGTRAP` in
`-[_VZVirtualMachineAccessor addAccessorObserver:]` under VNC connection
churn (crash logs `~/Library/Logs/DiagnosticReports/tart-*.ips`). A bare
socket open that only reads the banner (no full handshake) also leaves a
half-open session. Always complete the handshake (through `ServerInit`) or
don't connect at all.

In code: `ghosthands.vnc.open_session()` returns one connected `RFBClient`;
pass it as `rfb=` to `VNCHands`, `VNCEyes`, and `vnc_navigate`. `run.py`
does this for every `GH_HANDS=vnc` run and closes the session in a
`finally` block.

## Guest auto-login

`/etc/kcpassword` (root:wheel, 0600) + `autoLoginUser=admin` in
`/Library/Preferences/com.apple.loginwindow.plist`. After a guest reboot,
`/dev/console` becomes `admin` ~40 s after boot. Verified 2026-09-21.

## Known quirks

- **Modifier keys**: Tart's `_VZVNCServer` does not map X11 keysyms the way
  macOS Screen Sharing does. `Meta_L` (0xFFE7) arrives as **Option**, not
  Command. Plain typing and Shift work; Command-key chords via VNC are
  unreliable. Prefer mouse clicks, or drive app launching over guest SSH
  (`ssh admin@192.168.64.3 "open -a Safari <url>"`).
- **Guest Screen Sharing (port 5900) is NOT a substitute**: connecting to it
  logs the console user out (`/dev/console` flips `admin` → `root`).
- **Resolution**: 1920×1080 once the desktop is up (1280×720 while booting).
- **Scroll direction**: guest set to traditional (not "natural") so positive
  scroll amounts page down, matching the other hands backends.

## Files

| Path | Purpose |
|---|---|
| `~/Library/LaunchAgents/com.brkd.tart.mac-gui.plist` | launchd agent |
| `~/Library/Logs/tart-mac-gui.log` | tart stdout; VNC URL per boot |
| `~/.config/ghosthands/tart-vnc-current.env` | current VNC endpoint (0600) |
| `~/.config/ghosthands/guest-login.env` | guest `admin` credential (0600) |
| `~/Development/ghosthands/bin/tart-vm-up` | bring-up helper |

No credential values are ever printed, logged, or committed.
