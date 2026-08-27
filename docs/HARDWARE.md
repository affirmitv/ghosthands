# Hands: build the USB-HID actuator

You need a **Raspberry Pi Pico** (RP2040, ~$4) flashed with CircuitPython + the firmware in
`scripts/firmware/`. Once flashed and plugged into the machine you want to drive, it appears to
that machine as an ordinary USB mouse + keyboard.

## Flash it
1. Unplug the Pico. Hold **BOOTSEL**, plug it in, release after ~2s → the `RPI-RP2` drive mounts.
2. Run `scripts/reflash-pico.sh` — it drops `cp-pico-*.uf2`, waits for `CIRCUITPY`, copies
   `boot.py` + `code.py` + `lib/`, and ejects.
3. Replug normally. `ls /dev/cu.usbmodem*` should show one port.

## Wiring / firmware notes (learned the hard way)
- **Slim USB config.** The RP2040 endpoint budget can't fit keyboard + absolute-mouse + two CDC +
  mass-storage at once. `boot.py` runs `usb_cdc.enable(console=True, data=False)` — one CDC that
  doubles as the command channel.
- **CircuitPython has no `bytearray` slice-delete.** The serial read buffer is trimmed with
  `_buf = _buf[nl+1:]` (reassign), never `del _buf[:nl+1]` (that raises on CircuitPython and
  silently kills the loop after the first command).
- **Editing in place doesn't work** while the custom HID is active — the `CIRCUITPY` drive drops.
  Every firmware change is a BOOTSEL re-flash.

## Command protocol
One JSON object per line over the serial port at 115200; one ack per command:
`{"move":{"x":0..32767,"y":0..32767}}` (absolute), `{"click":"left"}`, `{"type":"..."}`,
`{"key":["cmd","space"]}`, `{"scroll":n}`, `{"ping":1}`. Coordinates are absolute over a
0..32767 grid; `ghosthands` maps screen fractions onto that, so it is resolution-independent.
