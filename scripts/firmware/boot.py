# boot.py — USB shaping for the AppSpace Pico HID actuator (RP2040 / CircuitPython).
#
# WHY THIS FILE MUST BE boot.py (NOT code.py):
#   The USB device descriptor is frozen the instant CircuitPython finishes running
#   boot.py and brings USB up. usb_hid.enable([...]) and usb_cdc.enable(...) ONLY
#   take effect here — calling them from code.py is a no-op because USB is already
#   enumerated. This is the #1 gotcha of the whole rig.
#
# WHAT IT BUILDS (a single composite USB device that the host machine sees as ordinary
# external hardware — electrically indistinguishable from a human at the keyboard):
#   1. HID keyboard (the standard boot keyboard).
#   2. HID ABSOLUTE-positioning mouse (custom descriptor below): the brain commands
#      screen coordinates directly in a 0..32767 logical range, plus 3 buttons and a
#      wheel. Absolute (not relative) so "put the cursor at (x,y)" is one report.
#   3. ONE usb_cdc DATA serial channel (console disabled) that the brain writes
#      newline-delimited JSON commands into. The console REPL is intentionally OFF so
#      exactly ONE /dev/tty.usbmodem* appears on the host machine — port detection is trivial.
#
# Recovery note: if you ever need the REPL back to debug, flip CONSOLE below to True,
# save, and the board re-enumerates with a second serial port (the REPL).

import usb_hid
import usb_cdc
import supervisor

# Disguise the USB identity: the host should see an ordinary Logitech receiver
# (the most common combo keyboard+mouse dongle on earth — same shape as this
# device), NOT "Raspberry Pi". Must run in boot.py before USB comes up. Fail-open.
try:
    supervisor.set_usb_identification(
        manufacturer="Logitech", product="USB Receiver", vid=0x046D, pid=0xC52B
    )
except Exception:
    pass

# usb_midi hogs USB endpoints we don't need. Disabling it guarantees endpoint budget
# for KEYBOARD + absolute-mouse HID + the CDC data channel on the RP2040.
try:
    import usb_midi
    usb_midi.disable()
except Exception:
    pass

CONSOLE = True  # DEBUG BRING-UP: REPL on so code.py tracebacks are visible (2nd tty)

# Enable the command channel. console=False => single tty on the host = easy to find.
# data=True adds the raw serial the brain talks to as usb_cdc.data in code.py.
usb_cdc.enable(console=CONSOLE, data=False)  # slim: 1 CDC only (REPL) — fits RP2040 endpoints alongside keyboard+abs-mouse, keeps CIRCUITPY drive editable

# --- Custom ABSOLUTE mouse HID report descriptor -----------------------------------
# Layout of one input report (report_id 4), 6 bytes total, in this exact order:
#   byte 0     : buttons  -> bit0 Left, bit1 Right, bit2 Middle (5 bits padding)
#   bytes 1..2 : X        -> unsigned 16-bit LITTLE-endian, logical 0..32767 (ABSOLUTE)
#   bytes 3..4 : Y        -> unsigned 16-bit LITTLE-endian, logical 0..32767 (ABSOLUTE)
#   byte 5     : Wheel    -> signed 8-bit, -127..127 (RELATIVE)
# The ABS (not REL) flag on X/Y (Input 0x02 "Absolute") is the load-bearing bit that
# makes the host place the pointer at the reported coordinate.
_ABS_MOUSE_DESCRIPTOR = bytes((
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x02,        # Usage (Mouse)
    0xA1, 0x01,        # Collection (Application)
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x85, 0x04,        #     Report ID (4)
    #  --- 3 buttons (1 bit each) ---
    0x05, 0x09,        #     Usage Page (Button)
    0x19, 0x01,        #     Usage Minimum (Button 1)
    0x29, 0x03,        #     Usage Maximum (Button 3)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x95, 0x03,        #     Report Count (3)
    0x75, 0x01,        #     Report Size (1)
    0x81, 0x02,        #     Input (Data, Var, Abs)
    #  --- 5 bits padding to fill the button byte ---
    0x95, 0x01,        #     Report Count (1)
    0x75, 0x05,        #     Report Size (5)
    0x81, 0x03,        #     Input (Const, Var, Abs)
    #  --- X, Y : two 16-bit ABSOLUTE axes, logical 0..32767 ---
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x16, 0x00, 0x00,  #     Logical Minimum (0)
    0x26, 0xFF, 0x7F,  #     Logical Maximum (32767)
    0x75, 0x10,        #     Report Size (16 bits)
    0x95, 0x02,        #     Report Count (2)
    0x81, 0x02,        #     Input (Data, Var, Abs)
    #  --- Wheel : one 8-bit RELATIVE axis ---
    0x09, 0x38,        #     Usage (Wheel)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8 bits)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x06,        #     Input (Data, Var, Rel)
    0xC0,              #   End Collection (Physical)
    0xC0,              # End Collection (Application)
))

# Build the custom device and enable the composite HID interface.
# Fail-OPEN: if anything about the custom descriptor is rejected at enumeration,
# fall back to a known-good keyboard+standard-mouse so the board still mounts and the
# CIRCUITPY drive stays editable (never brick the actuator over a descriptor typo).
try:
    ABS_MOUSE = usb_hid.Device(
        report_descriptor=_ABS_MOUSE_DESCRIPTOR,
        usage_page=0x01,          # Generic Desktop
        usage=0x02,               # Mouse
        report_ids=(4,),          # matches "Report ID (4)" above
        in_report_lengths=(6,),   # 6 payload bytes (report id excluded)
        out_report_lengths=(0,),  # host->device: none
    )
    usb_hid.enable([usb_hid.Device.KEYBOARD, ABS_MOUSE])
except Exception:
    usb_hid.enable([usb_hid.Device.KEYBOARD, usb_hid.Device.MOUSE])
