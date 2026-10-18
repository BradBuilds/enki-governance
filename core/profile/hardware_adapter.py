"""
Hardware key adapter loader — bridge between the profile system and
the enki-hardware-key HWKEY adapter.

For profiles that require a hardware key (hardware, hardened), this module
returns a RootKey-shaped object backed by the HWKEY over USB serial.

For profiles that don't require it (dev, cloud, hub), this module is never
called.

The actual adapter implementation lives in the enki-hardware-key repo at
$ENKI_HARDWARE_KEY_PATH/server/hardware_key.py. This module is the loader
seam — keeps this codebase from importing across repos
unnecessarily.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("enki.profile.hardware_adapter")


class HardwareKeyNotConfigured(RuntimeError):
    """Raised when a hardware-required profile starts without the
    enki-hardware-key adapter wired up."""


def load_hardware_adapter():
    """Return a RootKey-shaped object backed by the HWKEY.

    Raises HardwareKeyNotConfigured if:
      - ENKI_HARDWARE_KEY_PATH is unset
      - enki-hardware-key/server/hardware_key.py is not on disk
      - The watch isn't plugged in or doesn't respond to ping
    """
    hw_path = os.environ.get("ENKI_HARDWARE_KEY_PATH", "")
    if not hw_path:
        raise HardwareKeyNotConfigured(
            "ENKI_HARDWARE_KEY_PATH is not set. The hardware/hardened "
            "profile requires this to point at the enki-hardware-key repo."
        )

    hw_root = Path(hw_path)
    if not (hw_root / "server" / "hardware_key.py").exists():
        raise HardwareKeyNotConfigured(
            f"enki-hardware-key not found at {hw_root}. "
            "Clone https://github.com/BradBuilds/enki-hardware-key into "
            "this path or set ENKI_HARDWARE_KEY_PATH."
        )

    if str(hw_root) not in sys.path:
        sys.path.insert(0, str(hw_root))

    try:
        from server.hardware_key import HardwareKeyAdapter  # type: ignore
    except ImportError as e:
        raise HardwareKeyNotConfigured(
            f"Could not import HardwareKeyAdapter from {hw_root}: {e}"
        ) from e

    port = os.environ.get("ENKI_HWKEY_PORT", "/dev/ttyACM0")
    try:
        adapter = HardwareKeyAdapter(port=port)
    except Exception as e:
        raise HardwareKeyNotConfigured(
            f"Could not open HWKEY at {port}: {e}. "
            "Plug in the watch (USB-C) and verify with: "
            "ls -la /dev/ttyACM* /dev/ttyUSB*"
        ) from e

    log.info("HardwareKeyAdapter ready (port=%s)", port)
    return adapter
