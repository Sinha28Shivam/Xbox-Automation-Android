"""
android.py - the SENSOR side: adb.

Everything here is OPTIONAL. adb may be missing, the phone may be unauthorised,
USB may be busy powering the Leonardo through an OTG adapter. So every method
returns a value plus a reason, and nothing raises. A run without adb is a valid
run - it is just blind, and the report says so.

xCLOUD IS A PWA - WHAT THAT CHANGES
-----------------------------------
There is no xCloud app. `pm list packages` will never show an "xcloud" package,
so the usual Android-automation moves are unavailable:

    * no `am start -n <pkg>/<activity>`  -> we send an intent for a URL instead
    * no package name to assert on       -> the focused window is a BROWSER
    * no version to read from the phone  -> the "build under test" is whatever
                                            the server served today

That last point is worth stating in every report: a PWA can change between two
runs with no local change at all, which makes an unexplained new failure a
genuinely plausible event rather than automatically our bug.

We therefore DISCOVER browsers (and any WebAPK) rather than assuming one, and
we treat "which browser" as data for the agents, not as a constant in code.
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

    # -- process plumbing --------------------------------------------------
    def _run(self, args: list[str], timeout: float = 20.0,
             binary: bool = False,
             merge_stderr: bool = False) -> tuple[bool, str | bytes]:
        """Run one adb command. Returns (ok, stdout-or-error-text).

        `merge_stderr` matters more than it looks. An on-device failure -
        `input text` hitting a SecurityException, for instance - writes its stack
        trace to STDERR and still exits ZERO. Callers that only read stdout
        therefore see empty output, a zero exit code, and conclude success. See
        `shell_checked`, which exists entirely to catch that.
        """
        if not self.adb:
            return False, "adb is not available"

        cmd = [self.adb]
        if self.serial:
            # Always target explicitly. With two devices attached, an untargeted
            # command fails; worse, it can silently hit the wrong phone.
            cmd += ["-s", self.serial]
        cmd += args
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  check=False)
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

    def shell_checked(self, command: str,
                      timeout: float = 20.0) -> tuple[bool, str]:
        """Like `shell`, but treats an on-device Java exception as FAILURE.

        WHY THIS EXISTS - a real silent failure, found on this rig
        ---------------------------------------------------------
        `adb shell input text ...` on this Xiaomi/MIUI phone prints:

            Exception occurred while executing 'text':
            java.lang.SecurityException: Injecting input events requires the
            caller ... to have the INJECT_EVENTS permission.

        ...and then EXITS ZERO. The `adb` process succeeded - it delivered the
        command and the shell ran - so `_run` sees returncode 0 and reports ok.
        The failure happened INSIDE the device, and the only trace of it is text
        on stderr.

        That is the project's own headline trap wearing different clothes: a
        layer reported success for something that did not happen, and every
        layer above it believed the report. `input_text` returned
        "injected text 'minecraft dungeons' via adb", the step was marked
        hardware_ok, and the search field stayed empty.

        So any command whose real outcome lives in its OUTPUT rather than its
        exit code must be read, not trusted. `_run` merges stderr into the
        returned text for exactly this reason.
        """
        ok, out = self._run(["shell", command], timeout, merge_stderr=True)
        text = out if isinstance(out, str) else ""
        if not ok:
            return False, text

        # The device-side failure signatures. Checked as substrings because the
        # surrounding stack trace changes between Android versions.
        for marker in ("SecurityException", "Exception occurred while executing",
                       "Permission denial", "java.lang.IllegalStateException"):
            if marker in text:
                first = next((l.strip() for l in text.splitlines()
                              if marker in l), marker)
                self.last_error = first
                return False, (f"the command ran but FAILED on the device: "
                               f"{first}")
        return True, text


    def shell_guarded(self, command: str) -> tuple[bool, str]:
        """For commands an LLM chose. Prefix-allowlisted, off by default.

        Kept separate from `shell` so a reader can see at a glance which call
        sites are model-driven and which are ours.
        """
        if not self.s.get("safety.allow_shell", False):
            return False, ("shell access is disabled (safety.allow_shell). "
                           "Enable it deliberately if a scenario needs it.")
        allowed = [str(p) for p in self.s.get_list("safety.allowed_shell_prefixes")]
        if not any(command.startswith(p) for p in allowed):
            return False, (f"command '{command}' does not start with an allowed "
                           f"prefix ({', '.join(allowed) or 'none configured'})")
        return self.shell(command)

    # -- discovery ---------------------------------------------------------
    def connect(self) -> AndroidStatus:
        """Find adb, pick a device, and learn what we can. Never raises."""
        configured = str(self.s.get("android.adb_path", "adb"))
        self.adb = shutil.which(configured) or (
            configured if Path(configured).is_file() else None)
        if not self.adb:
            self.status.error = (
                f"adb not found (looked for '{configured}' on PATH). Screen "
                f"observation and log capture are unavailable; input still "
                f"works, because it goes over the Arduino, not adb.")
            return self.status

        self.status.adb_available = True
        ok, out = self._run(["version"], timeout=10.0)
        if ok and isinstance(out, str):
            first = out.strip().splitlines()[0] if out.strip() else ""
            self.status.adb_version = first

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
                self.status.error = (
                    f"configured device '{wanted}' is not attached; "
                    f"attached: {', '.join(d[0] for d in devices) or 'none'}")
                return False
            self.serial, state = match
        elif not devices:
            self.status.error = (
                "no adb device attached. On this rig that is common and often "
                "expected: the phone's USB port is busy acting as HOST for the "
                "Leonardo, so it cannot also be an adb client over the same "
                "cable. Use adb over Wi-Fi (`adb tcpip 5555`) to get eyes.")
            return False
        elif len(devices) > 1:
            # Refuse to guess. Picking one at random is how you spend an hour
            # debugging a "flaky" test that was hitting a different phone.
            self.status.error = (
                f"{len(devices)} devices attached "
                f"({', '.join(d[0] for d in devices)}). Set android.serial in "
                f"config/agentic.yaml to choose one.")
            return False
        else:
            self.serial, state = devices[0]

        self.status.device_serial = self.serial
        self.status.device_state = state
        if state != "device":
            self.status.error = (
                f"device {self.serial} is in state '{state}'. "
                f"'unauthorized' means the USB-debugging prompt on the phone "
                f"has not been accepted; 'offline' usually needs a replug.")
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

        # A blank screen explains "the input did nothing" instantly, so it is
        # worth knowing before we start rather than after a failed run.
        ok, out = self.shell("dumpsys power", timeout=15.0)
        if ok:
            if "mWakefulness=Awake" in out:
                self.status.screen_on = True
            elif "mWakefulness=" in out:
                self.status.screen_on = False

        self.status.focused_window = self.focused_window()

    def _discover_launchers(self) -> None:
        """Find which browsers exist. Hints from config, decisions by the agent."""
        ok, out = self.shell("pm list packages", timeout=25.0)
        if not ok:
            return
        packages = [line.split(":", 1)[1].strip()
                    for line in out.splitlines() if ":" in line]

        hints = [str(h).lower() for h in
                 self.s.get_list("android.pwa.browser_hints",
                                 ["chrome", "edge", "firefox", "samsung"])]
        marker = str(self.s.get("android.pwa.webapk_marker", "webapk")).lower()

        self.status.browsers_found = sorted(
            p for p in packages if any(h in p.lower() for h in hints))
        # A WebAPK is how "install to home screen" materialises the PWA. Its
        # package name is generated per install, so it can only be discovered.
        self.status.webapks_found = sorted(
            p for p in packages if marker in p.lower())
        self.status.chosen_launcher = (self.status.browsers_found[0]
                                       if self.status.browsers_found else None)

    # -- observation -------------------------------------------------------
    def focused_window(self) -> str | None:
        """Which app is in front. For a PWA this is a BROWSER package, never
        anything called xcloud - the sole reliable identity signal we get."""
        ok, out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'",
                             timeout=15.0)
        if not ok or not out.strip():
            # `grep` is absent on some Android builds; fall back to filtering
            # the whole dump ourselves.
            ok, out = self.shell("dumpsys window windows", timeout=25.0)
            if not ok:
                return None
            out = "\n".join(l for l in out.splitlines()
                            if "mCurrentFocus" in l or "mFocusedApp" in l)
        match = re.search(r"mCurrentFocus=\S+\s+\S+\s+([^}]+)", out)
        if match:
            return match.group(1).strip()
        return out.strip().splitlines()[0].strip() if out.strip() else None

    def screencap(self, dest: Path) -> tuple[bool, str]:
        """PNG screenshot. `exec-out` avoids the CRLF mangling that makes
        `shell screencap -p` produce a corrupt file on Windows.

        Every failure path logs, because "no screenshot available" is the single
        most destructive silent outcome in this project: without a frame there is
        no frame diff, so every verdict is capped at inconclusive and the reader
        is left unable to tell "the app did not react" from "we never looked".
        """
        ok, data = self._run(["exec-out", "screencap", "-p"], timeout=30.0,
                             binary=True)
        if not ok or not isinstance(data, bytes) or not data:
            detail = f"screencap failed: {self.last_error or 'empty output'}"
            log.error(f"NO SCREENSHOT - {detail}. Without a frame this step "
                      f"cannot be judged from pixels at all.", indent=1)
            return False, detail
        if not data.startswith(b"\x89PNG"):
            detail = ("screencap did not return a PNG (the device may "
                      "block capture on protected content - DRM-protected "
                      "video often yields a black or refused frame)")
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
        """Recent log buffer. `-d` dumps and exits, so this cannot hang."""
        count = int(lines or self.s.get("logs.logcat_lines", 400))
        ok, out = self._run(["logcat", "-d", "-v", "time", "-t", str(count)],
                            timeout=30.0)
        return out if ok and isinstance(out, str) else ""

    def clear_logcat(self) -> bool:
        """Clear before a step so the excerpt afterwards is about THAT step."""
        ok, _ = self._run(["logcat", "-c"], timeout=15.0)
        return ok

    def relevant_log_lines(self, raw: str, limit: int = 40) -> list[str]:
        """Score lines by configured patterns.

        A filter, never a gate: the raw buffer is kept too, because the line that
        explains a failure is never in the list you thought of in advance.
        """
        patterns = [str(p).lower() for p in
                    self.s.get_list("logs.interesting_patterns")]
        if not patterns:
            return raw.splitlines()[-limit:]
        hits = [l for l in raw.splitlines()
                if any(p in l.lower() for p in patterns)]
        return hits[-limit:]

    # -- actions -----------------------------------------------------------
    def launch_pwa(self, url: str | None = None,
                   package: str | None = None) -> tuple[bool, str]:
        """Open the xCloud URL. A VIEW intent, because a PWA has no activity.

        Passing a package pins it to one browser; omitting it lets Android
        resolve, which may show a chooser dialog - so we prefer the discovered
        browser when we have one.
        """
        target = url or str(self.s.get("android.pwa.url",
                                       "https://www.xbox.com/play"))
        pkg = package or self.status.chosen_launcher
        cmd = f"am start -a android.intent.action.VIEW -d '{target}'"
        if pkg:
            cmd += f" -p {pkg}"
        ok, out = self.shell(cmd, timeout=30.0)
        if not ok:
            return False, f"could not launch {target}: {out}"
        if "Error" in out or "Exception" in out:
            return False, f"Android rejected the intent: {out.strip()}"
        detail = f"sent VIEW intent for {target}"
        detail += f" to {pkg}" if pkg else (
            " with no package pinned - Android may have shown a chooser, which "
            "would swallow the gamepad input that follows")
        return True, detail

    # The advice printed whenever event injection is refused. Kept in one place
    # because both `keyevent` and `input_text` fail for the identical reason and
    # a reader hitting either needs the same three options.
    _INJECT_DENIED_HELP = (
        "\n\n  WHY: `adb shell input` asks the system to INJECT an input event, "
        "which needs the INJECT_EVENTS permission. The adb shell user (uid 2000) "
        "does not hold it on this device. Xiaomi/MIUI in particular gates this "
        "behind a setting, and it is NOT the same switch as USB debugging."
        "\n\n  WHAT WORKS:"
        "\n    1. On the phone, enable Developer options -> 'USB debugging "
        "(Security settings)' / 'Allow granting permissions and simulating "
        "input'. On MIUI this requires being signed into a Mi account and can "
        "take a few minutes to become available. This is the real fix."
        "\n    2. Use an IME-based keyboard instead of event injection. "
        "io.appium.settings is already installed here and ships AppiumIME, but "
        "SELECTING it needs `settings put secure default_input_method`, which "
        "needs WRITE_SECURE_SETTINGS - also denied. So it must be chosen BY HAND "
        "on the phone first (Settings -> Languages & input -> Keyboard)."
        "\n    3. Accept the limitation and reach the game WITHOUT typing: "
        "navigate the library/rails with the D-pad. Slower, and it tests the "
        "controller more honestly, since no adb is involved at all.")

    def can_inject_events(self) -> tuple[bool, str]:
        """Probe ONCE whether event injection is permitted at all.

        Worth doing up front rather than discovering it mid-run: if injection is
        denied, every `input` command will fail the same way, and a scenario that
        depends on typing cannot start. That makes it a BLOCKED precondition, not
        a test failure - a distinction the report has to get right.
        """
        # A harmless keycode. 0 is KEYCODE_UNKNOWN: it changes nothing on the
        # device but still travels the whole injection path, so a refusal here
        # means a refusal everywhere.
        ok, detail = self.shell_checked("input keyevent 0", timeout=15.0)
        if ok:
            return True, "adb can inject input events"
        return False, detail

    def keyevent(self, key: str) -> tuple[bool, str]:
        """A phone key (BACK, HOME, WAKEUP, ENTER). Distinct from GAMEPAD input,
        which must go through the Arduino - injected key events are not HID
        reports and prove nothing about the controller path.

        Uses `shell_checked`, because `input keyevent` exits ZERO even when the
        device refuses it.
        """
        ok, out = self.shell_checked(f"input keyevent {key}", timeout=15.0)
        if ok:
            return True, f"injected keyevent {key} via adb"
        log.error(f"adb keyevent {key} was REFUSED by the device: {out}",
                  indent=1)
        return False, out + (self._INJECT_DENIED_HELP
                             if "INJECT_EVENTS" in out else "")

    def input_text(self, text: str) -> tuple[bool, str]:
        """Type into the focused field via adb. Returns (ok, detail).

        THIS IS THE METHOD THAT WAS LYING
        ---------------------------------
        It previously used `shell`, which only inspects the adb process's exit
        code. On this device `input text` prints a SecurityException and exits
        ZERO, so the old code returned:

            (True, "injected text 'minecraft dungeons' via adb")

        while the search field stayed completely empty. The step was recorded as
        hardware_ok, the plan carried on pressing DOWN and A against a page that
        had never been searched, and the eventual failure pointed at the wrong
        layer entirely.

        Two changes fix it: `shell_checked` reads the output instead of trusting
        the exit code, and the returned detail carries the actual remedy rather
        than a stack trace.

        Note also that this is NOT gamepad input. Even when it works it proves
        nothing about the controller path - it is a fixture, and the report must
        never count it as controller evidence.
        """
        raw = str(text)
        # `input text` treats the argument as a single token, so a literal space
        # would be read as a second argument and silently truncate the string.
        # %s is the documented escape.
        escaped = raw.replace(" ", "%s")

        ok, out = self.shell_checked(f"input text '{escaped}'", timeout=15.0)
        if ok:
            log.adb(f"typed '{raw}' into the focused field via adb", indent=2)
            return True, (f"injected text '{raw}' via adb. NOTE: this is adb, "
                          f"not the gamepad, so it is not evidence about the "
                          f"controller path")

        detail = out
        if "INJECT_EVENTS" in out:
            detail += self._INJECT_DENIED_HELP
        log.error(f"adb could NOT type '{raw}' - the device refused the "
                  f"injection. The field is still empty, so any later step that "
                  f"assumes a search was performed is testing the wrong screen.",
                  indent=1)
        return False, detail


