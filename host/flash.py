"""
flash.py - One-click flasher for the Leonardo xCloud gamepad firmware.

Called by FLASH.bat. Handles everything that can go wrong, so the .bat can stay
a one-liner:

  1. Checks arduino-cli exists, and installs the AVR core if missing.
  2. Checks the CORRECT Joystick library is installed - and installs it from
     GitHub if not. This matters: `arduino-cli lib install "Joystick"` gives you
     a DIFFERENT library by another author that reads physical thumbsticks. The
     HID-emulation one we need (MHeironimus) is not in the Arduino index at all,
     and installing the wrong one produces a wall of
     `'Joystick_' does not name a type` errors.
  3. Compiles to a temp dir so the .hex is ready before the board is touched.
  4. Finds the Leonardo, waiting for the RESET bootloader window if needed.
  5. Flashes with avrdude DIRECTLY.

Why avrdude directly instead of `arduino-cli upload`: the Leonardo's bootloader
lives for only ~8 SECONDS after a RESET, and arduino-cli's own startup (loading
config, resolving the platform, re-checking the sketch) costs 1-3 s of that. In
testing that overhead was enough to lose the window - the port had already
vanished by the time avrdude ran. Pre-building and invoking avrdude ourselves
removes the delay entirely.

USAGE
    python flash.py
    python flash.py --timeout 120
    python flash.py --port COM12      (skip detection)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

REPO = Path(__file__).resolve().parent.parent
SKETCH = REPO / "firmware" / "leonardo_gamepad"
FQBN = "arduino:avr:leonardo"

BOOTLOADER_IDS = {"2341:0036", "2A03:0036"}   # Leonardo in bootloader
SKETCH_IDS = {"2341:8036", "2A03:8036"}       # Leonardo running a sketch

JOYSTICK_URL = ("https://github.com/MHeironimus/ArduinoJoystickLibrary"
                "/archive/refs/heads/master.zip")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def step(n: int, total: int, msg: str) -> None:
    print(f"[{n}/{total}] {msg}")


# --------------------------------------------------------------------------
# Prerequisites
# --------------------------------------------------------------------------
def have_arduino_cli() -> bool:
    if shutil.which("arduino-cli") is None:
        print("      arduino-cli NOT FOUND on PATH.")
        print()
        print("      Install it from https://arduino.github.io/arduino-cli/")
        print("      or with:   winget install ArduinoSA.CLI")
        return False
    res = run(["arduino-cli", "version"])
    print(f"      {res.stdout.strip().splitlines()[0] if res.stdout else 'ok'}")
    return True


def ensure_avr_core() -> bool:
    res = run(["arduino-cli", "core", "list"])
    if "arduino:avr" in (res.stdout or ""):
        print("      arduino:avr core present")
        return True
    print("      arduino:avr core missing - installing (this takes a minute) ...")
    res = run(["arduino-cli", "core", "install", "arduino:avr"])
    if res.returncode != 0:
        print("      FAILED to install the AVR core:")
        print((res.stderr or res.stdout or "").strip()[:500])
        return False
    print("      installed")
    return True


def joystick_lib_ok() -> bool:
    """True only for MHeironimus's HID-emulation library.

    Both libraries are called "Joystick". We identify the right one by looking
    for `Joystick_` (the class we actually use) in its header, rather than by
    version number, which is a far more reliable test.
    """
    res = run(["arduino-cli", "lib", "list"])
    if "Joystick" not in (res.stdout or ""):
        return False
    for base in (Path.home() / "Documents" / "Arduino" / "libraries",
                 Path.home() / "OneDrive" / "Documents" / "Arduino" / "libraries"):
        hdr = base / "Joystick" / "src" / "Joystick.h"
        if hdr.is_file():
            try:
                return "Joystick_" in hdr.read_text(errors="replace")
            except OSError:
                pass
    return False


def ensure_joystick_lib() -> bool:
    if joystick_lib_ok():
        print("      Joystick library (MHeironimus HID) present")
        return True

    print("      Correct Joystick library missing - installing from GitHub ...")
    print("      (the Library Manager's 'Joystick' is a DIFFERENT library that")
    print("       reads physical thumbsticks; it cannot emulate a gamepad)")

    res = run(["arduino-cli", "lib", "list"])
    if "Joystick" in (res.stdout or ""):
        print("      removing the wrong 'Joystick' library first ...")
        run(["arduino-cli", "lib", "uninstall", "Joystick"])

    zip_path = Path(tempfile.gettempdir()) / "joystick_lib.zip"
    res = run(["curl", "-sL", "-o", str(zip_path), JOYSTICK_URL])
    if res.returncode != 0 or not zip_path.is_file():
        print("      FAILED to download. Check your internet connection, or")
        print(f"      download manually from {JOYSTICK_URL}")
        return False

    run(["arduino-cli", "config", "set", "library.enable_unsafe_install", "true"])
    res = run(["arduino-cli", "lib", "install", "--zip-path", str(zip_path)])
    zip_path.unlink(missing_ok=True)
    if res.returncode != 0:
        print("      FAILED to install:")
        print((res.stderr or res.stdout or "").strip()[:500])
        return False
    print("      installed")
    return True


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------
def build_hex() -> Path | None:
    outdir = Path(tempfile.gettempdir()) / "xcloudpad_build"
    res = run(["arduino-cli", "compile", "--fqbn", FQBN,
               "--output-dir", str(outdir), str(SKETCH)])
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0:
        print()
        print("      COMPILE FAILED - your board has NOT been touched.")
        print()
        for line in out.splitlines():
            if line.strip():
                print(f"      {line.rstrip()}")
        if "does not name a type" in out:
            print()
            print("      This is the wrong-Joystick-library error. Try:")
            print("        arduino-cli lib uninstall Joystick")
            print("      then run this script again to reinstall the right one.")
        return None

    for line in out.splitlines():
        if "program storage" in line or "dynamic memory" in line:
            print(f"      {line.strip()}")

    hexes = list(outdir.glob("*.ino.hex"))
    return hexes[0] if hexes else None


def find_avrdude() -> tuple[Path, Path] | None:
    base = (Path.home() / "AppData" / "Local" / "Arduino15" /
            "packages" / "arduino" / "tools" / "avrdude")
    exes = sorted(base.glob("*/bin/avrdude.exe"))
    confs = sorted(base.glob("*/etc/avrdude.conf"))
    if not exes or not confs:
        return None
    return exes[-1], confs[-1]


# --------------------------------------------------------------------------
# Board discovery + flash
# --------------------------------------------------------------------------
def find_leonardo() -> tuple[str | None, str | None]:
    ids: dict[str, str] = {}
    for p in list_ports.comports():
        if p.vid is not None and p.pid is not None:
            ids[f"{p.vid:04X}:{p.pid:04X}"] = p.device
    for want in BOOTLOADER_IDS:
        if want in ids:
            return ids[want], "bootloader"
    for want in SKETCH_IDS:
        if want in ids:
            return ids[want], "sketch"
    return None, None


def wait_for_board(timeout: float) -> tuple[str | None, str | None]:
    port, kind = find_leonardo()
    if port:
        print(f"      found in {kind} mode on {port}")
        return port, kind

    print()
    print("      The Leonardo is not visible yet.")
    print()
    print("      >>> TAP THE RESET BUTTON ON THE LEONARDO NOW <<<")
    print()
    print("      (If it is running the Android gamepad or GIMX firmware it may")
    print("       not appear until you do. Tapping RESET twice quickly helps -")
    print("       the second press restarts the ~8 second window.)")
    print()
    print(f"      Waiting up to {timeout:.0f}s ", end="", flush=True)

    deadline = time.time() + timeout
    ticks = 0
    while time.time() < deadline:
        port, kind = find_leonardo()
        if port:
            print()
            print(f"      detected {kind} on {port}")
            return port, kind
        time.sleep(0.2)
        ticks += 1
        if ticks % 5 == 0:
            print(".", end="", flush=True)
    print()
    return None, None


def flash(port: str, hexfile: Path, avrdude: Path, conf: Path) -> bool:
    cmd = [str(avrdude), "-C", str(conf), "-p", "atmega32u4", "-c", "avr109",
           "-P", f"\\\\.\\{port}", "-b", "57600", "-D",
           "-U", f"flash:w:{hexfile}:i"]
    res = run(cmd)
    out = (res.stdout or "") + (res.stderr or "")
    for line in out.splitlines():
        s = line.strip()
        # Skip avrdude's progress bars - pure noise.
        if s and not s.startswith("#") and "|" not in s:
            print(f"      {s}")
    return res.returncode == 0


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Flash the xCloud gamepad firmware.")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--port", default=None, help="skip board detection")
    args = ap.parse_args()

    print("=" * 68)
    print("  FLASH THE LEONARDO - xCloud gamepad firmware")
    print("=" * 68)
    print()

    if not SKETCH.is_dir():
        print(f"ERROR: sketch folder missing: {SKETCH}")
        return 2

    TOTAL = 5

    step(1, TOTAL, "Checking arduino-cli ...")
    if not have_arduino_cli():
        return 1

    step(2, TOTAL, "Checking the AVR core and Joystick library ...")
    if not ensure_avr_core():
        return 1
    if not ensure_joystick_lib():
        return 1

    step(3, TOTAL, "Compiling ...")
    hexfile = build_hex()
    if hexfile is None:
        return 1
    tools = find_avrdude()
    if tools is None:
        print("      ERROR: avrdude not found in the AVR core.")
        print("      Try:  arduino-cli core install arduino:avr")
        return 1
    print(f"      built {hexfile.name}")

    step(4, TOTAL, "Finding the Leonardo ...")
    if args.port:
        port, kind = args.port, None
        print(f"      using {port} (specified)")
    else:
        port, kind = wait_for_board(args.timeout)
        if port is None:
            print()
            print("      TIMED OUT - no Leonardo appeared.")
            print("        * Is it connected to THIS PC by its own USB cable?")
            print("        * Is that a DATA cable? Charge-only shows nothing.")
            print("        * Try tapping RESET twice in quick succession.")
            return 1

    step(5, TOTAL, f"Flashing {port} ...")
    ok = flash(port, hexfile, tools[0], tools[1])
    if not ok and kind == "sketch":
        # In sketch mode the board needs a 1200-baud touch to enter the
        # bootloader. avrdude cannot do that itself; arduino-cli can.
        print("      retrying via arduino-cli so it can do the 1200-baud reset ...")
        res = run(["arduino-cli", "upload", "-p", port, "--fqbn", FQBN,
                   str(SKETCH)])
        ok = res.returncode == 0
        for line in ((res.stdout or "") + (res.stderr or "")).splitlines():
            if line.strip():
                print(f"      {line.rstrip()}")

    print()
    if not ok:
        print("=" * 68)
        print("  FLASH FAILED")
        print("=" * 68)
        print("Almost always the ~8 second bootloader window closing before")
        print("avrdude could open the port. Just run this again and tap RESET")
        print("when prompted.")
        print()
        print("Your board is NOT bricked - the bootloader cannot be erased, so")
        print("RESET always gives you another window.")
        return 1

    print("=" * 68)
    print("  SUCCESS - firmware flashed")
    print("=" * 68)
    print()
    print("  NEXT STEPS")
    print()
    print("  1. Unplug the Leonardo from the PC.")
    print("  2. Connect it to the PHONE:")
    print("         [Leonardo USB] --cable--> [OTG adapter] --> [PHONE]")
    print("                                   ^^ adapter at the PHONE end")
    print("  3. Check the Leonardo's ON LED lights - that proves the phone")
    print("     entered USB host mode and is powering the board.")
    print("  4. Make sure the FT232RL is wired and plugged into the PC:")
    print("         FT232RL TX  -> Leonardo RX (D0)")
    print("         FT232RL RX  -> Leonardo TX (D1)")
    print("         FT232RL GND -> Leonardo GND")
    print("  5. Run  RUN.bat  (or CHECK.bat first to confirm the link).")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
