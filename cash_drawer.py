"""
Cash Drawer control via serial port with USB cash.
Works drawers that appear as /dev/ttyUSB* (Prolific, CH340, etc.)
"""

import serial
import threading
import os

# Default device - can be configured via environment variable
DEFAULT_DEVICE = os.getenv("CASH_DRAWER_DEVICE", "/dev/ttyUSB0")

# ESC/POS command to open cash drawer (standard)
# ESC p m t1 t2 - Pulse drawer kick
CASH_DRAWER_OPEN = b'\x1b\x70\x00\x19\xfb'


def trigger_cash_drawer(device=None, timeout=1):
    """
    Open the cash drawer connected to the specified serial port.
    Runs in a background thread to not block the main app.
    """
    # Use default if device is None
    if device is None:
        device = DEFAULT_DEVICE

    def _open_drawer():
        try:
            with serial.Serial(device, 9600, timeout=timeout) as ser:
                ser.write(CASH_DRAWER_OPEN)
                ser.flush()
                print(f"💰 Cash drawer triggered on {device}")
        except Exception as e:
            print(f"❌ Cash drawer error on {device}: {e}")

    # Run in background thread
    thread = threading.Thread(target=_open_drawer, daemon=True)
    thread.start()
    return True


def is_drawer_available():
    """Check if the cash drawer device exists."""
    return os.path.exists(DEFAULT_DEVICE)
