"""
pad_link.py - Universal, config-driven controller for Android xCloud.

    this script -> USB serial -> Arduino -> BLE/USB HID -> Android phone -> xCloud

This is the Android counterpart of Xbox-Automation-Python/test-controller.
The public API is deliberately IDENTICAL, so anything written against
`ConsolePad` works here with a one-line change:

    from pad_link import AndroidPad
    pad = AndroidPad()
    pad.press("a")
    pad.press_times("down", 3)
    pad.press_times("right", 5, interval=0.4)
    pad.hold("guide", 2.0)
    pad.stick("left_stick", "right", duration=1.0)
    pad.trigger("rt", 255, duration=0.5)
    pad.run_macro("nav_test")

WHAT IS DIFFERENT FROM THE CONSOLE VERSION
------------------------------------------
1. No GIMX, no UDP, no separate long-lived server process. The transport is one
   persistent pyserial connection owned by this object. Consequently there is
   also no `gimx_session.py` equivalent and NO Guide-button authentication -
   Android needs none.

2. Because the link is persistent, a press costs ~2 ms instead of the ~250 ms
   that spawning gimx.exe cost per event.

3. Every command gets a REPLY (`OK` / `ERR <reason>`). The console path wrote
   bytes and hoped. Here a failure is reported with a reason, which is why
   `_send` can distinguish "bad button name" from "phone disconnected".

4. The D-pad is a HAT, not four buttons - Android UI navigation requires it.
   `press("down")` therefore emits `H 180` / `H C`, transparently.

CLI:
    python pad_link.py --list
    python pad_link.py --check
    python pad_link.py press a
    python pad_link.py press down*3 right a
    python pad_link.py hold guide 2.0
    python pad_link.py stick left_stick right --duration 1.0
    python pad_link.py trigger rt 255
    python pad_link.py macro nav_test
    python pad_link.py reset
    python pad_link.py --interactive
    python pad_link.py --dry-run press a

HONEST LIMITATION
-----------------
`OK` proves the firmware queued an HID report. It does NOT prove xCloud
reacted. Prove the HID layer with a gamepad-tester app first; prove xCloud
reacted with a screen capture (scrcpy) - same honesty rule as the console
project, where "GIMX accepted the event" never meant "the console moved".
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required:  pip install pyyaml")

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

DEFAULT_CONFIG_PATH = (Path(__file__).resolve().parent.parent
                       / "config" / "controls.yaml")



# ==========================================================================
# Config
# ==========================================================================
class ControlConfig:
    """Loads xcloud_controls.yaml and resolves friendly names -> hid names."""

    def __init__(self, path: Path | str = DEFAULT_CONFIG_PATH):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"Control config not found: {self.path}")
        with self.path.open("r", encoding="utf-8") as fh:
            self.data: dict[str, Any] = yaml.safe_load(fh) or {}

        self.buttons = self.data.get("buttons", {}) or {}
        self.triggers = self.data.get("triggers", {}) or {}
        self.sticks = self.data.get("sticks", {}) or {}
        self.timing = self.data.get("timing", {}) or {}
        self.macros = self.data.get("macros", {}) or {}
        self.special = self.data.get("special_actions", {}) or {}
        self.transports = self.data.get("transports", {}) or {}
        self.connection = self.data.get("connection", {}) or {}

        self._alias_map = self._build_alias_map()

    def _build_alias_map(self) -> dict[str, tuple[str, str]]:
        """alias/name -> (kind, canonical) where kind is button|trigger."""
        amap: dict[str, tuple[str, str]] = {}
        for kind, table in (("button", self.buttons), ("trigger", self.triggers)):
            for canonical, spec in table.items():
                amap[canonical.lower()] = (kind, canonical)
                for alias in (spec.get("aliases") or []):
                    amap.setdefault(str(alias).lower(), (kind, canonical))
        return amap

    def resolve(self, name: str) -> tuple[str, str]:
        key = str(name).strip().lower()
        if key not in self._alias_map:
            known = ", ".join(sorted(self._alias_map))
            raise KeyError(f"Unknown control '{name}'. Known: {known}")
        return self._alias_map[key]

    def is_hat(self, canonical: str) -> bool:
        """True for D-pad entries, which carry a `hat:` angle in the YAML."""
        return "hat" in (self.buttons.get(canonical) or {})

    def timing_value(self, key: str, fallback: float) -> float:
        try:
            return float(self.timing.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def transport_profile(self, name: str | None = None) -> dict[str, Any]:
        if name:
            if name not in self.transports:
                raise KeyError(f"Unknown transport '{name}'. "
                               f"Known: {', '.join(self.transports)}")
            return self.transports[name]
        for spec in self.transports.values():
            if spec.get("default"):
                return spec
        if self.transports:
            return next(iter(self.transports.values()))
        raise KeyError("No transport profiles defined in config")


# ==========================================================================
# Serial transport
# ==========================================================================
class PadLink:
    """Owns the serial connection to the Arduino and the line protocol.

    Kept separate from AndroidPad for the same reason gimx_session.py was kept
    separate from test_controller.py: holding the port is a different concern
    from deciding which buttons to press, and only ONE process may hold it.
    """

    def __init__(self, cfg: ControlConfig, port: str | None = None,
                 dry_run: bool = False):
        self.cfg = cfg
        conn = cfg.connection
        self.baudrate = int(conn.get("baudrate", 115200))
        self.boot_wait = float(conn.get("boot_wait", 1.5))
        self.timeout = float(conn.get("command_timeout", 2.0))
        self.ping_retries = int(conn.get("ping_retries", 5))
        self.usb_ids = [str(s).upper() for s in (conn.get("usb_ids") or [])]

        self.port = port or conn.get("serial_port")   # may be None -> autodetect
        self.dry_run = dry_run
        self.ser: serial.Serial | None = None
        self.firmware: str | None = None
        self.transport: str | None = None
        self.pad_connected: bool | None = None

    # -- port discovery ----------------------------------------------------
    def find_port(self) -> str | None:
        """Locate the board by USB VID:PID, never by remembered COM number.

        The console project was bitten badly by trusting a positional index: a
        capture-card index silently became a webcam and every check happily
        agreed the console was off. COM numbers move the same way, so we match
        on identity instead.
        """
        ports = list(list_ports.comports())
        if not ports:
            return None

        by_id: dict[str, str] = {}
        for p in ports:
            if p.vid is not None and p.pid is not None:
                by_id[f"{p.vid:04X}:{p.pid:04X}"] = p.device

        for wanted in self.usb_ids:                    # priority order
            if wanted in by_id:
                return by_id[wanted]

        # Fall back to a description that smells like a dev board.
        for p in ports:
            desc = f"{p.description} {p.manufacturer or ''}".lower()
            if any(k in desc for k in
                   ("ch340", "cp210", "silicon labs", "esp32", "arduino",
                    "leonardo", "usb serial")):
                return p.device
        return None

    @staticmethod
    def list_ports_verbose() -> list[Any]:
        ports = list(list_ports.comports())
        print("Serial ports present:")
        if not ports:
            print("  (none)")
        for p in ports:
            ident = (f"{p.vid:04X}:{p.pid:04X}"
                     if p.vid is not None and p.pid is not None else "?")
            print(f"  {p.device:<8} {ident:<10} {p.description}")
        return ports

    # -- lifecycle ---------------------------------------------------------
    def open(self, quiet: bool = False) -> bool:
        if self.dry_run:
            self.firmware, self.transport, self.pad_connected = \
                "dry-run", "dry-run", True
            return True

        if not self.port:
            self.port = self.find_port()
        if not self.port:
            print("ERROR: no Arduino serial port found.")
            self.list_ports_verbose()
            print("\nChecks: USB DATA cable (not charge-only), board powered,")
            print("and the Arduino Serial Monitor CLOSED (it holds the port -")
            print("the same single-owner rule GIMX had with COM8).")
            return False

        try:
            self.ser = serial.Serial(self.port, self.baudrate,
                                     timeout=self.timeout, write_timeout=2.0)
        except serial.SerialException as exc:
            print(f"ERROR: cannot open {self.port}: {exc}")
            msg = str(exc)
            if "not functioning" in msg or "Cannot configure port" in msg:
                # Distinct from "busy". The port EXISTS and Windows insists the
                # device is healthy, but it cannot be configured - `mode COMx`
                # shows Baud: 0 / Data Bits: 0. Seen repeatedly on this Due
                # after a reflash or an abrupt disconnect. No process is holding
                # it, so hunting for one wastes time.
                print()
                print("  This is NOT a busy port - the endpoint has wedged.")
                print("  Windows will still report the device as healthy, but")
                print("  `mode {}` shows Baud: 0 / Data Bits: 0.".format(self.port))
                print()
                print("  FIX: unplug the board's USB cable, wait 3 seconds, and")
                print("       plug it back in. A software reset cannot clear")
                print("       this; the USB endpoint has to be re-enumerated.")
                print()
                print("  (If you were mid-flash, the flash itself likely")
                print("   succeeded - re-run --check after replugging.)")
            else:
                print("  Most likely another process holds it. Close the")
                print("  Arduino Serial Monitor, or kill a stale pad_link /")
                print("  hammer_test / watch_usb process.")
            return False


        # Opening the port toggles DTR, which RESETS an ESP32 / Leonardo. Talking
        # before it finishes booting looks exactly like broken firmware.
        if not quiet:
            print(f"Opened {self.port} @ {self.baudrate} - "
                  f"waiting {self.boot_wait:.1f}s for the board to boot ...")
        time.sleep(self.boot_wait)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

        return self.ping(quiet=quiet)

    def ping(self, quiet: bool = False) -> bool:
        """Handshake. Retries because the boot banner can still be in flight."""
        if self.dry_run:
            return True
        for _ in range(self.ping_retries):
            reply = self._transact("PING")
            if reply and reply.startswith("PONG"):
                parts = reply.split()
                self.firmware = parts[1] if len(parts) > 1 else "?"
                self.transport = parts[2] if len(parts) > 2 else "?"
                self.pad_connected = (len(parts) > 3 and parts[3] == "1")
                if not quiet:
                    print(f"  firmware  : {self.firmware}")
                    print(f"  transport : {self.transport}")
                    print(f"  pad linked to phone : "
                          f"{'YES' if self.pad_connected else 'NO'}")
                    if not self.pad_connected:
                        print("  >> The board is alive but the PHONE is not")
                        print("     connected. Pair/reconnect it in Android")
                        print("     Bluetooth settings, then re-run --check.")
                return True
            time.sleep(0.3)
        print("ERROR: no PONG from the board.")
        print("  Wrong sketch flashed, wrong baudrate, or wrong port.")
        return False

    def close(self) -> None:
        """Always release inputs before dropping the link.

        Without this, a crash mid-`stick()` leaves an axis deflected and the
        character keeps walking into a wall forever.
        """
        if self.ser and self.ser.is_open:
            try:
                self._transact("RESET")
            except (serial.SerialException, OSError):
                pass
            self.ser.close()
        self.ser = None

    # -- protocol ----------------------------------------------------------
    def _transact(self, line: str) -> str | None:
        """Write one command, read exactly one reply line."""
        if self.dry_run:
            return "OK"
        if not self.ser or not self.ser.is_open:
            return None
        try:
            self.ser.reset_input_buffer()
            self.ser.write((line + "\n").encode("ascii"))
            self.ser.flush()
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                raw = self.ser.readline().decode("ascii", "replace").strip()
                if not raw:
                    continue
                # The firmware emits unsolicited EVENT/READY lines; skip them
                # so a BLE reconnect notice never gets mistaken for our reply.
                if raw.startswith(("EVENT", "READY")):
                    print(f"  [board] {raw}")
                    continue
                return raw
            return None
        except (serial.SerialException, OSError) as exc:
            print(f"  SERIAL ERROR: {exc}")
            return None

    def send(self, line: str, label: str = "") -> bool:
        """Send a command and interpret the reply. True only on OK."""
        if self.dry_run:
            print(f"  [dry-run] {label or line:<18} -> {line}")
            return True

        reply = self._transact(line)
        if reply is None:
            print(f"   FAILED (no reply to '{line}')")
            return False
        if reply.startswith("OK"):
            return True
        if reply.startswith("ERR"):
            print(f"   FAILED: {reply}")
            if "not connected" in reply:
                print("       >> no host has enumerated the pad. On USB check")
                print("          the OTG wiring; on BLE re-pair the device.")
            elif "unknown button" in reply:
                print("       >> the YAML `hid:` name is not in the sketch's")
                print("          BTN_TABLE. Fix one to match the other.")
            return False

        # Anything else is a garbled or unexpected reply. Sanitise before
        # printing: a corrupted UART byte can decode to a character the Windows
        # console codepage (cp1252) cannot encode, and printing it raw raises
        # UnicodeEncodeError - crashing the tool over a display concern rather
        # than reporting the actual problem. Seen for real on the FTDI link.
        safe = reply.encode("ascii", "backslashreplace").decode("ascii")
        print(f"   UNEXPECTED reply: {safe}")
        if "\\x" in safe or "?" in safe:
            print("       >> that looks like CORRUPTED serial data, not a")
            print("          protocol error. On a UART link (FT232RL -> D0/D1)")
            print("          the usual causes are:")
            print("            * no common GND between the two boards")
            print("            * the FTDI's voltage jumper set to 3.3V")
            print("              instead of 5V")
            print("            * long or unshielded jumper wires")
        return False


    def state(self) -> str | None:
        return self._transact("STATE")

    def reset(self) -> bool:
        return self.send("RESET", "reset")

    def __enter__(self) -> "PadLink":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ==========================================================================
# The pad
# ==========================================================================
class AndroidPad:
    """Sends controller input to an Android phone via the Arduino HID pad."""

    def __init__(self, config: ControlConfig | None = None,
                 transport: str | None = None, port: str | None = None,
                 dry_run: bool = False,
                 config_path: Path | str = DEFAULT_CONFIG_PATH,
                 auto_open: bool = True):
        self.cfg = config or ControlConfig(config_path)
        self.profile = self.cfg.transport_profile(transport)
        self.dry_run = dry_run
        self.link = PadLink(self.cfg, port, dry_run)

        self.gap = self.cfg.timing_value("gap_between_presses", 0.12)
        self.failed = False
        self.action_log: list[dict[str, Any]] = []
        self.opened = self.link.open() if auto_open else False

    # -- bookkeeping -------------------------------------------------------
    def _record(self, name: str, cmd: str, status: str) -> None:
        self.action_log.append({"time": time.time(), "name": name,
                                "cmd": cmd, "status": status})

    def _send(self, cmd: str, label: str) -> bool:
        ok = self.link.send(cmd, label)
        self._record(label, cmd, "sent" if ok else "failed")
        if not ok:
            self.failed = True
        return ok

    # -- buttons -----------------------------------------------------------
    def press(self, name: str, duration: float | None = None) -> bool:
        """Press and release a button (or fully actuate a trigger)."""
        kind, canonical = self.cfg.resolve(name)
        if duration is None:
            duration = self.cfg.timing_value("press_duration", 0.10)

        if kind == "trigger":
            spec = self.cfg.triggers[canonical]
            return self.trigger(canonical, spec.get("default_press", 255),
                                duration)

        spec = self.cfg.buttons[canonical]

        # D-pad -> HAT. Android UI navigation ignores generic buttons in many
        # titles, so this routing is not cosmetic.
        if self.cfg.is_hat(canonical):
            angle = int(spec["hat"])
            print(f"  -> {canonical} (hat {angle}) {duration:.2f}s",
                  end="", flush=True)
            if not self._send(f"H {angle}", canonical):
                return False
            time.sleep(duration)
            ok = self._send("H C", f"{canonical}:center")
        else:
            hid = spec["hid"]
            print(f"  -> {canonical} ({hid}) {duration:.2f}s", end="", flush=True)
            if not self._send(f"B {hid} 1", canonical):
                return False
            time.sleep(duration)
            ok = self._send(f"B {hid} 0", f"{canonical}:release")

        print("   ok" if ok else "")
        time.sleep(self.gap)
        return ok

    def press_times(self, name: str, times: int = 1,
                    duration: float | None = None,
                    interval: float | None = None) -> bool:
        """Press a button N times (e.g. move down 3 rows in a menu).

        `interval` is the pause BETWEEN repeats. xCloud menus animate AND add
        network latency, so they eat fast repeats more readily than a local
        console - raise this if steps go missing.
        """
        times = max(1, int(times))
        for i in range(times):
            if times > 1:
                print(f"  [{i + 1}/{times}]", end=" ")
            if not self.press(name, duration):
                return False
            if interval is not None and i < times - 1:
                time.sleep(float(interval))
        return True

    @staticmethod
    def parse_repeat(token: str) -> tuple[str, int]:
        """Parse repeat syntax: down*3, down x3, down:3, 3*down, or plain down."""
        t = str(token).strip()
        for sep in ("*", "x", "X", ":"):
            if sep in t:
                left, _, right = t.partition(sep)
                left, right = left.strip(), right.strip()
                if right.isdigit() and left:
                    return left, int(right)
                if left.isdigit() and right:
                    return right, int(left)
        return t, 1

    def hold(self, name: str, duration: float) -> bool:
        return self.press(name, duration=duration)

    def tap(self, name: str) -> bool:
        return self.press(name, self.cfg.timing_value("tap_duration", 0.06))

    def long_press(self, name: str) -> bool:
        return self.press(name, self.cfg.timing_value("long_press_duration", 1.0))

    def deep_press(self, name: str) -> bool:
        return self.press(name, self.cfg.timing_value("deep_press_duration", 2.0))

    # -- triggers ----------------------------------------------------------
    def trigger(self, name: str, value: int | None = None,
                duration: float | None = None) -> bool:
        """Analog trigger, 0..255."""
        _, canonical = self.cfg.resolve(name)
        spec = self.cfg.triggers.get(canonical)
        if spec is None:
            print(f"  ! '{name}' is not a trigger")
            return False
        if value is None:
            value = spec.get("default_press", 255)
        lo, hi = spec.get("min", 0), spec.get("max", 255)
        value = max(lo, min(hi, int(value)))
        if duration is None:
            duration = self.cfg.timing_value("press_duration", 0.10)

        # '-' keeps the other trigger untouched, so pulling RT never releases LT.
        pull = f"T {value} -" if spec["hid"] == "lt" else f"T - {value}"
        rel = f"T {lo} -" if spec["hid"] == "lt" else f"T - {lo}"

        print(f"  -> {canonical} = {value} for {duration:.2f}s", end="", flush=True)
        if not self._send(pull, canonical):
            return False
        time.sleep(duration)
        ok = self._send(rel, f"{canonical}:release")
        print("   ok" if ok else "")
        time.sleep(self.gap)
        return ok

    # -- sticks ------------------------------------------------------------
    def stick(self, stick_name: str, direction: str | None = None,
              x: int | None = None, y: int | None = None,
              duration: float | None = None) -> bool:
        """Move a stick by named direction or explicit x/y (-128..127)."""
        spec = self.cfg.sticks.get(stick_name)
        if spec is None:
            print(f"  ! Unknown stick '{stick_name}'. "
                  f"Known: {', '.join(self.cfg.sticks)}")
            return False
        if duration is None:
            duration = self.cfg.timing_value("press_duration", 0.10)

        axes: dict[str, int] = {}
        if direction:
            dirs = spec.get("directions", {})
            if direction not in dirs:
                print(f"  ! Unknown direction '{direction}'. "
                      f"Known: {', '.join(dirs)}")
                return False
            axes[dirs[direction]["axis"]] = int(dirs[direction]["value"])
        else:
            lo, hi = spec.get("min", -128), spec.get("max", 127)
            if x is not None:
                axes[spec["x_axis"]] = max(lo, min(hi, int(x)))
            if y is not None:
                axes[spec["y_axis"]] = max(lo, min(hi, int(y)))
        if not axes:
            print("  ! stick() needs a direction or x/y")
            return False

        center = spec.get("center", 0)
        label = direction or f"x={x},y={y}"
        print(f"  -> {stick_name} {label} for {duration:.2f}s", end="", flush=True)

        # One S command sets all four axes at once, so the phone never sees a
        # half-updated stick position (an artefact the per-axis GIMX path had).
        if not self._send(self._axis_cmd(axes), f"{stick_name}:{label}"):
            return False
        time.sleep(duration)
        ok = self._send(self._axis_cmd({a: center for a in axes}),
                        f"{stick_name}:center")
        print("   ok" if ok else "")
        time.sleep(self.gap)
        return ok

    @staticmethod
    def _axis_cmd(axes: dict[str, int]) -> str:
        """Build 'S <lx> <ly> <rx> <ry>', '-' for axes we are not touching."""
        order = ("lx", "ly", "rx", "ry")
        return "S " + " ".join(
            str(axes[a]) if a in axes else "-" for a in order)

    def center_sticks(self) -> bool:
        return self._send("S 0 0 0 0", "center_sticks")

    # -- sequences ---------------------------------------------------------
    def sequence(self, steps: list[dict[str, Any]]) -> bool:
        """Run a list of steps: {button|trigger|stick|wait, ...}."""
        for step in steps:
            if "wait" in step:
                time.sleep(float(step["wait"]))
                continue
            if "button" in step:
                if not self.press_times(step["button"],
                                        step.get("times", step.get("repeat", 1)),
                                        step.get("duration"),
                                        step.get("interval")):
                    return False
            elif "trigger" in step:
                if not self.trigger(step["trigger"], step.get("value"),
                                    step.get("duration")):
                    return False
            elif "stick" in step:
                if not self.stick(step["stick"], step.get("direction"),
                                  step.get("x"), step.get("y"),
                                  step.get("duration")):
                    return False
            else:
                print(f"  ! unrecognized step: {step}")
                return False
        return True

    def run_macro(self, macro_name: str) -> bool:
        macro = self.cfg.macros.get(macro_name)
        if macro is None:
            print(f"  ! Unknown macro '{macro_name}'. "
                  f"Known: {', '.join(self.cfg.macros)}")
            return False
        print(f"\n=== macro: {macro_name} - {macro.get('description', '')} ===")
        return self.sequence(macro.get("steps", []))

    def run_special(self, action_name: str) -> bool:
        action = self.cfg.special.get(action_name)
        if action is None:
            print(f"  ! Unknown action '{action_name}'. "
                  f"Known: {', '.join(self.cfg.special)}")
            return False
        if not action.get("verified", False):
            print(f"  NOTE: '{action_name}' is NOT hardware-verified - "
                  f"the sequence is a best guess.")
        print(f"\n=== action: {action_name} - {action.get('description','')} ===")
        return self.sequence(action.get("sequence", []))

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.link.close()

    def __enter__(self) -> "AndroidPad":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ==========================================================================
# CLI
# ==========================================================================
def print_controls(cfg: ControlConfig) -> None:
    print("=" * 64)
    print(f"CONTROL MAP   ({cfg.path})")
    print("=" * 64)
    print("\nBUTTONS            HID        ALIASES")
    for name, spec in cfg.buttons.items():
        kind = f"{spec['hid']}*" if "hat" in spec else spec["hid"]
        print(f"  {name:<16} {kind:<10} {', '.join(spec.get('aliases', []))}")
    print("  (* = sent as an HID hat direction, not a button)")
    print("\nTRIGGERS (analog)  HID        RANGE")
    for name, spec in cfg.triggers.items():
        print(f"  {name:<16} {spec['hid']:<10} "
              f"{spec.get('min', 0)}..{spec.get('max', 255)}")
    print("\nSTICKS             AXES              DIRECTIONS")
    for name, spec in cfg.sticks.items():
        print(f"  {name:<16} {spec['x_axis']}/{spec['y_axis']:<10} "
              f"{', '.join(spec.get('directions', {}))}")
    print("\nMACROS")
    for name, spec in cfg.macros.items():
        print(f"  {name:<16} {spec.get('description', '')}")
    print("\nSPECIAL ACTIONS")
    for name, spec in cfg.special.items():
        mark = "" if spec.get("verified") else "  [UNVERIFIED]"
        print(f"  {name:<16} {spec.get('description', '')}{mark}")
    print("\nTRANSPORTS")
    for name, spec in cfg.transports.items():
        mark = "  (default)" if spec.get("default") else ""
        print(f"  {name:<16} {spec.get('description', '')}{mark}")
    print("\nTIMING (seconds)")
    for k, v in cfg.timing.items():
        print(f"  {k:<26} {v}")


def interactive(pad: AndroidPad) -> None:
    print("\nInteractive mode. Examples:")
    print("  a                 press A")
    print("  down*3            press Down 3 times")
    print("  down 3            same thing")
    print("  down*2 right a    combine in one line")
    print("  hold guide 2      hold Guide 2s")
    print("  stick left right  move left stick right")
    print("  trigger rt 255    pull right trigger")
    print("  macro nav_test    run a macro")
    print("  state             ask the board what it is holding")
    print("  reset             release everything")
    print("  q                 quit\n")
    while True:
        try:
            line = input("pad> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.lower() in ("q", "quit", "exit"):
            break
        parts = line.split()
        verb = parts[0].lower()
        try:
            if verb == "state":
                print(f"  {pad.link.state()}")
            elif verb == "reset":
                pad.link.reset()
                print("  released everything")
            elif verb == "hold" and len(parts) >= 3:
                pad.hold(parts[1], float(parts[2]))
            elif verb == "stick" and len(parts) >= 3:
                name = (parts[1] if parts[1] in pad.cfg.sticks
                        else f"{parts[1]}_stick")
                pad.stick(name, parts[2])
            elif verb == "trigger" and len(parts) >= 2:
                pad.trigger(parts[1],
                            int(parts[2]) if len(parts) > 2 else None)
            elif verb == "macro" and len(parts) >= 2:
                pad.run_macro(parts[1])
            elif verb == "action" and len(parts) >= 2:
                pad.run_special(parts[1])
            else:
                idx = 0
                while idx < len(parts):
                    nm, count = AndroidPad.parse_repeat(parts[idx])
                    if (count == 1 and idx + 1 < len(parts)
                            and parts[idx + 1].isdigit()):
                        count = int(parts[idx + 1])
                        idx += 1
                    pad.press_times(nm, count)
                    idx += 1
        except KeyError as exc:
            print(f"  ! {exc}")
        except ValueError as exc:
            print(f"  ! bad number: {exc}")
    print("bye")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Config-driven controller for Android xCloud via Arduino HID.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--transport", default=None,
                    help="transport profile (default: the one marked default)")
    ap.add_argument("--port", default=None,
                    help="serial port, e.g. COM12 (default: auto-detect)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print commands without opening the port")
    ap.add_argument("--list", action="store_true", help="show the control map")
    ap.add_argument("--check", action="store_true",
                    help="open the link and report firmware + phone status")
    ap.add_argument("--interactive", action="store_true")

    sub = ap.add_subparsers(dest="command")

    p_press = sub.add_parser(
        "press", help="press buttons; supports repeats like down*3 or down 3")
    p_press.add_argument("names", nargs="+")
    p_press.add_argument("--times", type=int, default=None,
                         help="repeat EVERY listed button N times")
    p_press.add_argument("--interval", type=float, default=None)
    p_press.add_argument("--duration", type=float, default=None)

    p_hold = sub.add_parser("hold", help="hold a button for N seconds")
    p_hold.add_argument("name")
    p_hold.add_argument("duration", type=float)

    p_stick = sub.add_parser("stick", help="move a stick")
    p_stick.add_argument("stick_name")
    p_stick.add_argument("direction", nargs="?", default=None)
    p_stick.add_argument("--x", type=int, default=None)
    p_stick.add_argument("--y", type=int, default=None)
    p_stick.add_argument("--duration", type=float, default=None)

    p_trig = sub.add_parser("trigger", help="pull an analog trigger")
    p_trig.add_argument("name")
    p_trig.add_argument("value", type=int, nargs="?", default=None)
    p_trig.add_argument("--duration", type=float, default=None)

    p_macro = sub.add_parser("macro", help="run a macro from the config")
    p_macro.add_argument("name")

    p_action = sub.add_parser("action", help="run a special action")
    p_action.add_argument("name")

    sub.add_parser("reset", help="release every button and centre the sticks")
    sub.add_parser("state", help="ask the board what it is currently holding")
    sub.add_parser("ports", help="list serial ports and exit")

    args = ap.parse_args()

    try:
        cfg = ControlConfig(args.config)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.list:
        print_controls(cfg)
        return 0

    if args.command == "ports":
        PadLink.list_ports_verbose()
        return 0

    try:
        pad = AndroidPad(cfg, args.transport, args.port, args.dry_run)
    except KeyError as exc:
        print(f"ERROR: {exc}")
        return 2

    if not pad.opened and not args.dry_run:
        return 1

    try:
        if args.check:
            # ping() already printed the detail; restate the honest caveat.
            print("\nLink is up. Reminder: this proves the BOARD answers and")
            print("whether the PHONE is attached. It does NOT prove xCloud")
            print("reacted - check a gamepad-tester app, then the game screen.")
            return 0

        if args.interactive:
            interactive(pad)
            return 1 if pad.failed else 0

        if args.command == "press":
            # Accepts "down*3 a" and "down 3 a": a bare integer applies to the
            # button token before it.
            tokens: list[list[Any]] = []
            for raw in args.names:
                if raw.isdigit() and tokens:
                    tokens[-1][1] = int(raw)
                    continue
                nm, count = AndroidPad.parse_repeat(raw)
                tokens.append([nm, count])
            for nm, count in tokens:
                if args.times is not None:
                    count = args.times
                if not pad.press_times(nm, count, args.duration, args.interval):
                    break
        elif args.command == "hold":
            pad.hold(args.name, args.duration)
        elif args.command == "stick":
            pad.stick(args.stick_name, args.direction, args.x, args.y,
                      args.duration)
        elif args.command == "trigger":
            pad.trigger(args.name, args.value, args.duration)
        elif args.command == "macro":
            pad.run_macro(args.name)
        elif args.command == "action":
            pad.run_special(args.name)
        elif args.command == "reset":
            pad.link.reset()
            print("Released every button and centred the sticks.")
        elif args.command == "state":
            print(pad.link.state())
        else:
            ap.print_help()
            print("\nTip: start with  --list  then  --check")
            return 1
    except KeyError as exc:
        print(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        print("\nCtrl+C - releasing inputs ...")
    finally:
        # ALWAYS release. A held stick outlives this process otherwise.
        pad.close()

    return 1 if pad.failed else 0


if __name__ == "__main__":
    sys.exit(main())
