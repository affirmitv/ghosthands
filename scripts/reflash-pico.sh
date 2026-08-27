#!/bin/bash
# reflash-pico.sh — one-shot Pico re-flash on the host machine once it's in RPI-RP2 bootloader.
# Waits for the bootloader drive, drops CircuitPython on it, waits for CIRCUITPY,
# copies the debug firmware, ejects. Tolerant of the drive vanishing mid-write
# (the RP2040 reboots the instant the UF2 lands — that's expected).
set -u
FLASH=~/pico-flash
UF2="$FLASH/cp-pico-10.2.1.uf2"

wait_vol () { # $1=volume name  $2=timeout secs
  local name="$1" t="${2:-60}" i=0
  while [ $i -lt "$t" ]; do
    # try to mount any matching-but-unmounted disk first
    local node
    node=$(diskutil list 2>/dev/null | awk -v n="$name" '$0 ~ n {print $NF}' | grep -E '^disk[0-9]' | head -1)
    [ -n "$node" ] && diskutil mount "$node" >/dev/null 2>&1
    [ -d "/Volumes/$name" ] && { echo "  $name mounted"; return 0; }
    sleep 1; i=$((i+1))
  done
  return 1
}

echo "[1/4] waiting for RPI-RP2 bootloader (BOOTSEL replug now)..."
if ! wait_vol "RPI-RP2" 90; then echo "  RPI-RP2 never appeared — did the BOOTSEL button stay held during plug-in?"; exit 1; fi

echo "[2/4] flashing CircuitPython..."
cp "$UF2" /Volumes/RPI-RP2/ 2>/dev/null
sync
echo "  UF2 copied (board is rebooting to CircuitPython)"

echo "[3/4] waiting for CIRCUITPY..."
if ! wait_vol "CIRCUITPY" 90; then echo "  CIRCUITPY never appeared after flash"; exit 1; fi
sleep 2

echo "[4/4] copying debug firmware..."
mkdir -p /Volumes/CIRCUITPY/lib
cp -R "$FLASH/lib/adafruit_hid" /Volumes/CIRCUITPY/lib/
cp "$FLASH/boot.py" /Volumes/CIRCUITPY/
cp "$FLASH/code.py" /Volumes/CIRCUITPY/
sync
ls -la /Volumes/CIRCUITPY/
echo "  ejecting..."
diskutil eject /Volumes/CIRCUITPY 2>/dev/null
echo "DONE — board will re-enumerate with the debug firmware (REPL on)."
