"""
timing.py - EVERY deliberate wait in the project goes through here.

WHY THIS MODULE EXISTS - the bug it is a fix for
-----------------------------------------------
Run 20260817-105323 failed, and the RCA blamed the harness, correctly:

    step_07 macro nav_test        -> screen changed 3.257%  (input DID arrive)
    step_08 wait                  -> screen changed 0.074%
    step_09 observe "show evidence
            of gamepad navigation" -> screen changed 0.074%  -> FAIL

Nothing was broken on the phone. xCloud's selection highlight animated, and the
only observation that mattered was taken AFTER it had settled - so the proof was
photographed and then thrown away. The run was 338s long and the report's own
next action was "capture immediately after the input, before any wait".

Two design faults made that possible, and this module fixes both:

1. Waits were scattered `time.sleep(...)` calls with the number chosen at each
   call site - one in executor._dispatch, one in executor.run, one in
   pad.py, one hidden in a WAIT step. Nothing could report how long a run slept
   or why, so "add a wait after every click" had no single place to be added.

2. There was exactly ONE observation per step, taken at ONE moment. Any single
   moment is the wrong moment for something that animates: too early and you
   photograph the BEFORE state, too late and you photograph the settled state.

THE SETTLE PROFILE - a wait that knows what it is waiting for
-------------------------------------------------------------
`settle_for(kind)` returns the pair of delays appropriate to what just happened,
read from config (`execution.settle.*`) with the rig's own controls.yaml timing
as the fallback. A button press and a stream launch are not the same event and
must not share one magic number:

    press/hold/macro   short  - a menu highlight moves in ~0.4s
    stick/trigger      short  - analog, but the UI reaction is still a menu move
    launch_pwa         long   - a page load over the network
    special            medium - compound sequences, unknown by nature

WHY TWO DELAYS PER ACTION (`glance` then `settle`)
--------------------------------------------------
The fix the report asked for is not "wait longer" - waiting longer is what LOST
the evidence. It is to look TWICE:

    glance_delay   just past the input latency (~100ms network + a frame or two)
                   -> catches the transient: a highlight mid-animation, a
                      "Starting your game" toast, a flash of a dialog
    settle_delay   after the animation completes
                   -> the stable state a judgement can actually be made about

A step now passes if EITHER frame moved. That is strictly more honest: the claim
being tested is "the input reached xCloud", and a transient highlight proves it
exactly as well as a permanent one. Under the old single-look rule, step_07's
3.257% would have been discarded for the same reason step_09's 0.074% was.

Everything here is pure and side-effect free apart from sleeping and logging,
which is what makes the waits auditable: `log.wait(seconds, reason)` means the
transcript and the report can both answer "where did the 338 seconds go".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .logbook import log
from .schemas import ActionKind

# --------------------------------------------------------------------------
# Defaults. Deliberately conservative, and every one is overridable in
# config/agentic.yaml under `execution.settle`.
#
# The `glance` numbers are floored, not guessed: controls.yaml documents 60-100ms
# of xCloud network latency, so anything below ~0.25s photographs the BEFORE
# state and manufactures the exact false failure this module exists to prevent.
# --------------------------------------------------------------------------
DEFAULT_GLANCE = 0.45
DEFAULT_SETTLE = 1.5
MIN_GLANCE = 0.25

# kind -> (glance key, settle key, default glance, default settle)
_PROFILES: dict[str, tuple[str, str, float, float]] = {
    "press":      ("press.glance",   "press.settle",   0.45, 1.20),
    "hold":       ("hold.glance",    "hold.settle",    0.45, 1.50),
    "macro":      ("macro.glance",   "macro.settle",   0.60, 2.00),
    "stick":      ("stick.glance",   "stick.settle",   0.45, 1.20),
    "trigger":    ("trigger.glance", "trigger.settle", 0.45, 1.20),
    "special":    ("special.glance", "special.settle", 0.60, 2.00),
    "launch_pwa":   ("pwa.glance",     "pwa.settle",     1.50, 6.00),
    "adb_text":     ("press.glance",   "press.settle",   0.45, 1.50),
    "adb_keyevent": ("press.glance",   "press.settle",   0.45, 1.20),
    "reset":        ("reset.glance",   "reset.settle",   0.20, 0.40),
    "observe":      ("observe.glance", "observe.settle", 0.00, 0.30),
    "assert":       ("observe.glance", "observe.settle", 0.00, 0.30),
    "wait":         ("wait.glance",    "wait.settle",    0.00, 0.00),
}

# Which controls.yaml timing key is the most sensible fallback for each kind,
# so a rig that has tuned its own YAML is honoured before our defaults are.
_RIG_TIMING_HINT: dict[str, str] = {
    "press": "menu_transition_wait",
    "hold": "menu_transition_wait",
    "macro": "menu_transition_wait",
    "stick": "menu_transition_wait",
    "trigger": "menu_transition_wait",
    "special": "screen_load_wait",
    "launch_pwa": "screen_load_wait",
}


@dataclass(frozen=True)
class SettleProfile:
    """The two delays for one action, plus why they are those numbers.

    `reason` is carried rather than recomputed so the same string appears in the
    terminal log AND in the step's reasoning field - a reader should never have
    to guess whether the log and the report are describing the same wait.
    """
    kind: str
    glance: float
    settle: float
    reason: str = ""

    @property
    def total(self) -> float:
        return self.glance + self.settle

    def describe(self) -> str:
        return (f"glance {self.glance:.2f}s then settle {self.settle:.2f}s "
                f"(total {self.total:.2f}s)")


class Timing:
    """Resolves and performs every wait. One instance per run, on RunContext.

    Constructed with the Settings object and, optionally, the rig's own timing
    map from controls.yaml (`Capabilities.timing`), which is consulted before the
    built-in defaults - the rig knows its own latency better than we do.
    """

    def __init__(self, settings: Any, rig_timing: dict[str, float] | None = None):
        self.s = settings
        self.rig = dict(rig_timing or {})
        # Bookkeeping so the report can state where the wall-clock time went,
        # instead of leaving a reader to infer it from screenshot filenames.
        self.total_waited = 0.0
        self.waits: list[tuple[str, float, str]] = []
        self.scale = float(self.s.get("execution.settle.scale", 1.0) or 1.0)

    # -- resolution --------------------------------------------------------
    def _cfg(self, key: str, default: float) -> tuple[float, bool]:
        """Read one settle value. Returns (seconds, came_from_config).

        The flag matters: `reason` is quoted in the report, and a reason that
        credits controls.yaml for a number that actually came from agentic.yaml
        would send someone to edit the wrong file. Reporting the provenance
        wrongly is a smaller version of the same fault this module exists to fix.
        """
        value = self.s.get(f"execution.settle.{key}", None)
        if value is None:
            return float(default), False
        try:
            return max(0.0, float(value)), True
        except (TypeError, ValueError):
            # A typo in YAML must not stop a hardware run; use the default and
            # say so, loudly enough to be fixed.
            log.warn(f"execution.settle.{key}={value!r} is not a number - "
                     f"using the default {default}")
            return float(default), False

    def profile_for(self, kind: ActionKind | str) -> SettleProfile:
        """The wait profile for one action kind. Never raises."""
        name = (kind.value if isinstance(kind, ActionKind) else str(kind)).lower()
        glance_key, settle_key, glance_def, settle_def = _PROFILES.get(
            name, ("default.glance", "default.settle",
                   DEFAULT_GLANCE, DEFAULT_SETTLE))

        # Precedence: agentic.yaml > the rig's controls.yaml > our built-in.
        # The rig knows its own latency better than we do, so its tuned value
        # raises our default - but an explicit config entry always wins, because
        # someone typed it deliberately for this run.
        rig_source: str | None = None
        hint = _RIG_TIMING_HINT.get(name)
        if hint and hint in self.rig:
            widened = max(settle_def, float(self.rig[hint]))
            if widened != settle_def:
                rig_source = f"controls.yaml timing.{hint}={self.rig[hint]}"
            settle_def = widened

        glance, glance_from_cfg = self._cfg(glance_key, glance_def)
        settle, settle_from_cfg = self._cfg(settle_key, settle_def)
        glance *= self.scale
        settle *= self.scale

        if settle_from_cfg:
            source = f"execution.settle.{settle_key}"
        elif rig_source:
            source = rig_source
        else:
            source = "built-in default"
        if glance_from_cfg and not settle_from_cfg:
            source += f" (glance from execution.settle.{glance_key})"

        # Enforce the floor. A glance below the network latency is not a glance,
        # it is a photograph of the previous screen - the false-failure trap.
        if 0.0 < glance < MIN_GLANCE:
            glance = MIN_GLANCE
            source += f"; glance raised to the {MIN_GLANCE}s latency floor"

        return SettleProfile(
            kind=name, glance=round(glance, 3), settle=round(settle, 3),
            reason=f"{name}: {source}"
            + (f", scaled x{self.scale}" if self.scale != 1.0 else ""))


    # -- the primitives ----------------------------------------------------
    def sleep(self, seconds: float, reason: str, indent: int = 1) -> float:
        """THE only place this project sleeps. Returns what it actually slept.

        Centralised so that (a) every wait appears in the terminal log with the
        reason it happened, and (b) `execution.settle.scale` can slow a whole run
        down on a congested network without touching a dozen call sites.
        """
        duration = max(0.0, float(seconds or 0.0))
        if duration <= 0.0:
            return 0.0
        log.wait(duration, reason, indent=indent)
        started = time.monotonic()
        try:
            time.sleep(duration)
        except KeyboardInterrupt:
            # Let Ctrl-C through, but record the truncated wait: a run whose
            # timings are half-applied must not silently claim it waited.
            actual = time.monotonic() - started
            self._record(reason, actual)
            raise
        actual = time.monotonic() - started
        self._record(reason, actual)
        return actual

    def _record(self, reason: str, actual: float) -> None:
        self.total_waited += actual
        self.waits.append((reason.split(":")[0][:40], round(actual, 3), reason))

    def glance(self, profile: SettleProfile, indent: int = 1) -> float:
        """The short wait: just past input latency, before the UI settles."""
        return self.sleep(
            profile.glance,
            f"glance after {profile.kind} - catch the TRANSIENT reaction "
            f"(a highlight mid-animation) before it settles",
            indent=indent)

    def settle(self, profile: SettleProfile, indent: int = 1) -> float:
        """The longer wait: let the animation and the stream catch up."""
        return self.sleep(
            profile.settle,
            f"settle after {profile.kind} - let the UI animation and the "
            f"60-100ms stream latency finish",
            indent=indent)

    # -- named waits used outside the act/observe cycle --------------------
    def rig_wait(self, timing_key: str, default: float,
                 reason: str = "") -> float:
        """Wait a value named in controls.yaml (`stream_start_wait`, ...).

        Prefer this to a literal: the number then lives with the rig it belongs
        to, and a slower phone is a YAML edit rather than a code change.
        """
        seconds = float(self.rig.get(timing_key, default))
        return self.sleep(seconds,
                          reason or f"{timing_key} from controls.yaml")

    def poll_until(self, predicate: Callable[[], bool], timeout: float,
                   interval: float = 0.5, reason: str = "condition") -> bool:
        """Wait for something to become true, instead of sleeping blindly.

        Strictly better than a fixed sleep where a condition is checkable: it
        returns as soon as the thing happens, and it reports a TIMEOUT rather
        than continuing as though the wait had succeeded - a fixed sleep cannot
        tell those two outcomes apart, which is how "flaky" tests are born.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        log.wait(timeout, f"up to this long, polling every {interval:.2f}s "
                          f"for {reason}")
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            try:
                if predicate():
                    waited = timeout - (deadline - time.monotonic())
                    self._record(f"poll {reason}", waited)
                    log.ok(f"{reason} became true after {waited:.2f}s "
                           f"({attempts} checks)", indent=1)
                    return True
            except Exception as exc:                 # noqa: BLE001
                # A sensor that throws is a degraded sensor, not a failed test.
                log.debug(f"poll for {reason} raised {type(exc).__name__}: "
                          f"{exc}")
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        self._record(f"poll {reason} (timeout)", float(timeout))
        log.warn(f"{reason} did not become true within {timeout:.1f}s "
                 f"({attempts} checks) - continuing, and this is recorded")
        return False

    # -- reporting ---------------------------------------------------------
    def summary(self) -> str:
        """One line: how much of the run was spent deliberately waiting."""
        if not self.waits:
            return "no deliberate waits were performed"
        buckets: dict[str, float] = {}
        for label, seconds, _ in self.waits:
            buckets[label] = buckets.get(label, 0.0) + seconds
        top = sorted(buckets.items(), key=lambda kv: -kv[1])[:5]
        return (f"{self.total_waited:.1f}s spent in {len(self.waits)} "
                f"deliberate waits; largest: "
                + ", ".join(f"{k} {v:.1f}s" for k, v in top))
