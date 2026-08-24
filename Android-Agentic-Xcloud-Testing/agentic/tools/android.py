"""
android.py - the SENSOR side: adb.

Everything here is OPTIONAL. adb may be missing, the phone may be unauthorised,
USB may be busy powering the Leonardo through an OTG adapter. So every method
returns a value plus a reason, and nothing raises.

xCloud is a PWA, so browser discovery is used rather than assuming an xCloud
package exists.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..logbook import log
from ..schemas import AndroidStatus
from ..settings import Settings


class AndroidTool:
    """A thin, safe adb wrapper."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.status = AndroidStatus()
        self.adb: str | None = None
        self.serial: str | None = None
        self.last_error: str | None = None

    def _run(self, args: list[str], timeout: float = 20.0,
             binary: bool = False, merge_stderr: bool = False) -> tuple[bool, str | bytes]:
        if not self.adb:
            return False, "adb is not available"
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += args
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            self.last_error = f"adb timed out after {timeout}s: {' '.join(args)}"
            return False, self.last_error
        except OSError as exc:
            self.last_error = f"cannot execute adb: {exc}"
            return False, self.last_error
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            self.last_error = err or f"adb exited {proc.returncode}"
            return False, self.last_error
        if binary:
            return True, proc.stdout
        text = proc.stdout.decode("utf-8", "replace")
        if merge_stderr:
            err = proc.stderr.decode("utf-8", "replace")
            if err.strip():
                text = f"{text}\n{err}" if text.strip() else err
        return True, text

    def shell(self, command: str, timeout: float = 20.0) -> tuple[bool, str]:
        ok, out = self._run(["shell", command], timeout)
        return ok, out if isinstance(out, str) else ""

    def shell_checked(self, command: str, timeout: float = 20.0) -> tuple[bool, str]:
        """Run an adb shell command and detect device-side input failures."""
        ok, out = self._run(["shell", command], timeout, merge_stderr=True)
        text = out if isinstance(out, str) else ""
        if not ok:
            return False, text
        for marker in (
            "SecurityException",
            "Exception occurred while executing",
            "Permission denial",
            "java.lang.IllegalStateException",
        ):
            if marker in text:
                first = next((line.strip() for line in text.splitlines() if marker in line), marker)
                self.last_error = first
                return False, f"the command ran but FAILED on the device: {first}"
        return True, text

    def shell_guarded(self, command: str) -> tuple[bool, str]:
        if not self.s.get("safety.allow_shell", False):
            return False, "shell access is disabled (safety.allow_shell)"
        allowed = [str(p) for p in self.s.get_list("safety.allowed_shell_prefixes")]
        if not any(command.startswith(p) for p in allowed):
            return False, f"command '{command}' is not allow-listed"
        return self.shell(command)

    def connect(self) -> AndroidStatus:
        configured = str(self.s.get("android.adb_path", "adb"))
        self.adb = shutil.which(configured) or (configured if Path(configured).is_file() else None)
        if not self.adb:
            self.status.error = f"adb not found: {configured}"
            return self.status
        self.status.adb_available = True
        ok, out = self._run(["version"], timeout=10.0)
        if ok and isinstance(out, str):
            self.status.adb_version = out.strip().splitlines()[0] if out.strip() else ""
        if not self._pick_device():
            return self.status
        self._read_properties()
        self._discover_launchers()
        return self.status

    def _pick_device(self) -> bool:
        ok, out = self._run(["devices"], timeout=15.0)
        if not ok or not isinstance(out, str):
            self.status.error = f"`adb devices` failed: {self.last_error}"
            return False
        devices: list[tuple[str, str]] = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                devices.append((parts[0], parts[1]))
        wanted = self.s.get("android.serial")
        if wanted:
            match = next((d for d in devices if d[0] == str(wanted)), None)
            if match is None:
                self.status.error = f"configured device '{wanted}' is not attached"
                return False
            self.serial, state = match
        elif not devices:
            self.status.error = "no adb device attached; use adb over Wi-Fi if USB is occupied by the Leonardo"
            return False
        elif len(devices) > 1:
            self.status.error = f"{len(devices)} devices attached; set android.serial"
            return False
        else:
            self.serial, state = devices[0]
        self.status.device_serial = self.serial
        self.status.device_state = state
        if state != "device":
            self.status.error = f"device {self.serial} is in state '{state}'"
            return False
        return True

    def _read_properties(self) -> None:
        for prop, attr in (("ro.product.model", "model"),
                           ("ro.build.version.release", "android_version"),
                           ("ro.build.version.sdk", "sdk")):
            ok, out = self.shell(f"getprop {prop}", timeout=10.0)
            if ok and out.strip():
                setattr(self.status, attr, out.strip())
        ok, out = self.shell("wm size", timeout=10.0)
        if ok:
            match = re.search(r"(\d+x\d+)", out)
            if match:
                self.status.screen_size = match.group(1)
        ok, out = self.shell("dumpsys power", timeout=15.0)
        if ok:
            if "mWakefulness=Awake" in out:
                self.status.screen_on = True
            elif "mWakefulness=" in out:
                self.status.screen_on = False
        self.status.focused_window = self.focused_window()

    def _discover_launchers(self) -> None:
        ok, out = self.shell("pm list packages", timeout=25.0)
        if not ok:
            return
        packages = [line.split(":", 1)[1].strip() for line in out.splitlines() if ":" in line]
        hints = [str(h).lower() for h in self.s.get_list(
            "android.pwa.browser_hints", ["chrome", "edge", "firefox", "samsung"])]
        marker = str(self.s.get("android.pwa.webapk_marker", "webapk")).lower()
        self.status.browsers_found = sorted(
            p for p in packages if any(h in p.lower() for h in hints))
        self.status.webapks_found = sorted(p for p in packages if marker in p.lower())
        self.status.chosen_launcher = self.status.browsers_found[0] if self.status.browsers_found else None

    def focused_window(self) -> str | None:
        ok, out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", timeout=15.0)
        if not ok or not out.strip():
            ok, out = self.shell("dumpsys window windows", timeout=25.0)
            if not ok:
                return None
            out = "\n".join(l for l in out.splitlines() if "mCurrentFocus" in l or "mFocusedApp" in l)
        match = re.search(r"mCurrentFocus=\S+\s+\S+\s+([^}]+)", out)
        if match:
            return match.group(1).strip()
        return out.strip().splitlines()[0].strip() if out.strip() else None

    def screencap(self, dest: Path) -> tuple[bool, str]:
        ok, data = self._run(["exec-out", "screencap", "-p"], timeout=30.0, binary=True)
        if not ok or not isinstance(data, bytes) or not data:
            detail = f"screencap failed: {self.last_error or 'empty output'}"
            log.error(f"NO SCREENSHOT - {detail}", indent=1)
            return False, detail
        if not data.startswith(b"\x89PNG"):
            detail = "screencap did not return a PNG"
            log.error(f"NO SCREENSHOT - {detail}", indent=1)
            return False, detail
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError as exc:
            detail = f"cannot write {dest}: {exc}"
            log.error(f"NO SCREENSHOT - {detail}", indent=1)
            return False, detail
        log.adb(f"screencap -> {dest.name} ({len(data) // 1024} KB)", indent=2)
        return True, str(dest)

    def logcat(self, lines: int | None = None) -> str:
        count = int(lines or self.s.get("logs.logcat_lines", 400))
        ok, out = self._run(["logcat", "-d", "-v", "time", "-t", str(count)], timeout=30.0)
        return out if ok and isinstance(out, str) else ""

    def clear_logcat(self) -> bool:
        ok, _ = self._run(["logcat", "-c"], timeout=15.0)
        return ok

    def relevant_log_lines(self, raw: str, limit: int = 40) -> list[str]:
        patterns = [str(p).lower() for p in self.s.get_list("logs.interesting_patterns")]
        if not patterns:
            return raw.splitlines()[-limit:]
        return [line for line in raw.splitlines() if any(p in line.lower() for p in patterns)][-limit:]

    def launch_pwa(self, url: str | None = None, package: str | None = None) -> tuple[bool, str]:
        target = url or str(self.s.get("android.pwa.url", "https://www.xbox.com/play"))
        pkg = package or self.status.chosen_launcher
        cmd = f"am start -a android.intent.action.VIEW -d '{target}'"
        if pkg:
            cmd += f" -p {pkg}"
        ok, out = self.shell(cmd, timeout=30.0)
        if not ok:
            return False, f"could not launch {target}: {out}"
        if "Error" in out or "Exception" in out:
            return False, f"Android rejected the intent: {out.strip()}"
        return True, f"sent VIEW intent for {target}" + (f" to {pkg}" if pkg else "")

    _INJECT_DENIED_HELP = (
        "\n\nWHY: adb shell input requires INJECT_EVENTS on this device. "
        "WHAT WORKS: enable the device's security input-injection permission, "
        "or use an IME selected manually on the phone. If neither is possible, "
        "search-based launch is BLOCKED and the generic controller navigation "
        "fallback must be used.")

    def can_inject_events(self) -> tuple[bool, str]:
        ok, detail = self.shell_checked("input keyevent 0", timeout=15.0)
        return (True, "adb can inject input events") if ok else (False, detail)

    def keyevent(self, key: str) -> tuple[bool, str]:
        ok, out = self.shell_checked(f"input keyevent {key}", timeout=15.0)
        if ok:
            return True, f"injected keyevent {key} via adb"
        log.error(f"adb keyevent {key} was REFUSED by the device: {out}", indent=1)
        return False, out + (self._INJECT_DENIED_HELP if "INJECT_EVENTS" in out else "")

    def input_text(self, text: str) -> tuple[bool, str]:
        """Type into the focused field. `$input` resolves to XAT_RUNTIME_INPUT.

        The LLM never constructs this command. The value is resolved here so
        every ADB_TEXT action can use the same global runtime input token.
        """
        raw = str(text)
        if raw.strip() == "$input":
            raw = str(self.s.get("runtime.input", ""))
        if not raw:
            return False, "runtime input is empty; set XAT_RUNTIME_INPUT or runtime.input"

        escaped = raw.replace(" ", "%s")
        ok, out = self.shell_checked(f"input text '{escaped}'", timeout=15.0)
        if ok:
            log.adb(f"typed '{raw}' into the focused field via adb", indent=2)
            return True, (f"injected text '{raw}' via adb; this is fixture input, "
                          "not controller evidence")

        detail = out
        if "INJECT_EVENTS" in out:
            detail += self._INJECT_DENIED_HELP
        log.error(f"adb could NOT type '{raw}': {detail}", indent=1)
        return False, detail
