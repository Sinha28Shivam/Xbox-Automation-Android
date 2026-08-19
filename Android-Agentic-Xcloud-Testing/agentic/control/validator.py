"""
validator.py - every action passes through here before it reaches the board.

    Action -> schema -> capability -> policy -> safety -> range -> Executor

WHY THIS IS A SEPARATE MODULE AND NOT A PROMPT RULE
---------------------------------------------------
A prompt is a request; this is a wall. The distinction already existed in this
project (`pad.dispatch` enforces `safety.forbidden_controls` in code, and
`planner._sanitise` dropped invented controls), and the closed loop makes it
matter more, not less: there are now TWO producers of actions - the planner and
the decision agent - and a rule enforced in only one of them is not enforced.

WHAT IT CHECKS, IN ORDER
------------------------
1. CAPABILITY   the control must exist in ../config/controls.yaml, as discovered
                at runtime. This is the anti-hallucination check that stops
                `press("options")` on a pad that has no Options button.
2. POLICY       the scenario's own `controller_policy.prohibited_inputs`. This
                is what makes a "gamepad only" test honest: if the scenario says
                no ADB input, the harness must be unable to use it, not merely
                disinclined to. A test that quietly reaches its goal by a route
                it promised not to take has proved nothing about the route it
                claimed to be testing.
3. SAFETY       `safety.forbidden_controls`, for a device where Home hijacks.
4. RANGE        stick/trigger bounds and a maximum hold duration, so a
                malformed number cannot leave an axis deflected for a minute.
5. INVARIANT    navigation is ONE press. Enforced again here even though
                `Action.times` is already capped at 1 by its field constraint,
                because this is the layer that would have to catch it if that
                constraint were ever relaxed.

A rejected action is never silently dropped. `ValidationResult` carries the
reason, the caller logs it, and it lands in the report - a plan or a decision
that was quietly edited is a plan the report describes inaccurately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..schemas import (Action, ActionType, Capabilities, ScreenType)
from ..settings import Settings

# Absolute ceilings, independent of anything a model or a config asks for. These
# are about not leaving the rig in a damaging state, so they are constants.
MAX_HOLD_SECONDS = 10.0
MAX_WAIT_SECONDS = 120.0
STICK_MIN, STICK_MAX = -32768, 32767
TRIGGER_MIN, TRIGGER_MAX = 0, 255

# Action types that put a real HID report on the wire. Used to decide whether
# `can_send_input` is required.
_INPUT_TYPES = (ActionType.PRESS, ActionType.HOLD, ActionType.STICK,
                ActionType.TRIGGER, ActionType.MACRO, ActionType.RESET)

# The names a scenario may use in `prohibited_inputs` to ban a whole modality.
_MODALITY_BANS = {
    "adb_tap": ("adb", "touch"),
    "adb_keyevent": ("adb", "keyevent"),
    "adb_text": ("adb", "text"),
    "touchscreen_input": ("adb", "touch"),
    "virtual_keyboard": ("keyboard",),
    "browser_search": ("search",),
}


@dataclass
class ValidationResult:
    """The outcome of validating one action. Never a bare bool.

    `corrected` exists because some faults are worth fixing rather than
    refusing - a stick value of 40000 is obviously meant to be full deflection,
    and clamping it is more useful than discarding the decision. But a
    correction is recorded so the report shows what was actually sent, which is
    not always what was asked for.
    """
    ok: bool
    action: Action | None = None
    reason: str = ""
    corrections: list[str] = field(default_factory=list)

    @property
    def corrected(self) -> bool:
        return bool(self.corrections)


class ActionValidator:
    """Validates actions against capabilities, policy and safety."""

    def __init__(self, settings: Settings,
                 capabilities: Capabilities | None = None,
                 prohibited_inputs: list[str] | None = None):
        self.s = settings
        self.caps = capabilities or Capabilities()
        self.forbidden = {str(c).lower() for c in
                          settings.get_list("safety.forbidden_controls")}
        self.prohibited = {str(p).lower().strip()
                           for p in (prohibited_inputs or [])}

    # -- name sets ---------------------------------------------------------
    def _valid_names(self, action_type: ActionType) -> set[str]:
        caps = self.caps
        buttons = {b.lower() for b in caps.buttons}
        triggers = {t.lower() for t in caps.triggers}
        aliases = {a.lower() for a in caps.aliases}
        if action_type in (ActionType.PRESS, ActionType.HOLD):
            return buttons | triggers | aliases
        if action_type is ActionType.TRIGGER:
            return triggers | aliases
        if action_type is ActionType.STICK:
            return {s.lower() for s in caps.sticks}
        if action_type is ActionType.MACRO:
            return {m.lower() for m in caps.macros}
        return set()

    # -- main --------------------------------------------------------------
    def validate(self, action: Action,
                 screen: ScreenType | None = None) -> ValidationResult:
        """Check one action. Returns a copy, possibly with clamped values."""
        if action is None:
            return ValidationResult(False, reason="no action was produced")

        # Work on a copy: a validator that mutates its input makes "what did we
        # actually ask for?" unanswerable after the fact.
        act = action.model_copy(deep=True)
        corrections: list[str] = []

        # -- 1. non-input types need no control ------------------------
        if act.type in (ActionType.OBSERVE, ActionType.DONE):
            return ValidationResult(True, act)

        if act.type is ActionType.WAIT:
            seconds = float(act.seconds or act.duration or 1.0)
            if seconds > MAX_WAIT_SECONDS:
                corrections.append(
                    f"wait shortened from {seconds:.1f}s to the "
                    f"{MAX_WAIT_SECONDS:.0f}s ceiling: a longer blind wait "
                    f"hides whatever is happening on screen")
                seconds = MAX_WAIT_SECONDS
            if seconds < 0:
                corrections.append("negative wait treated as 0s")
                seconds = 0.0
            act.seconds = seconds
            act.duration = None
            return ValidationResult(True, act, corrections=corrections)

        if act.type is ActionType.LAUNCH_PWA:
            if not self.caps.can_launch_pwa:
                return ValidationResult(
                    False, reason="launching the PWA needs adb, which is not "
                                  "available this run")
            if not act.control:
                act.control = str(self.s.get("android.pwa.url",
                                             "https://www.xbox.com/play"))
                corrections.append(f"no URL given; using {act.control}")
            return ValidationResult(True, act, corrections=corrections)

        # -- 2. everything below sends input ---------------------------
        if act.type in _INPUT_TYPES and not self.caps.can_send_input:
            return ValidationResult(
                False, reason="this run has no input capability (the pad link "
                              "is not open), so no control can be sent")

        if act.type is ActionType.RESET:
            return ValidationResult(True, act)

        control = (act.control or "").strip().lower()
        if not control:
            return ValidationResult(
                False, reason=f"a {act.type.value} action needs a control name "
                              f"and none was given")

        # -- 3. capability fence ---------------------------------------
        valid = self._valid_names(act.type)
        if control not in valid:
            # Try the alias map before refusing: controls.yaml deliberately
            # offers friendly names, and rejecting one the rig itself publishes
            # would be the harness disagreeing with its own config.
            canonical = self.caps.aliases.get(control)
            if canonical and canonical.lower() in (
                    {b.lower() for b in self.caps.buttons}
                    | {t.lower() for t in self.caps.triggers}):
                corrections.append(
                    f"{control!r} resolved to {canonical!r} via the "
                    f"controls.yaml alias map")
                act.control = canonical
                control = canonical.lower()
            else:
                return ValidationResult(
                    False, reason=(
                        f"unknown {act.type.value} control {act.control!r}. "
                        f"Valid names come from controls.yaml: "
                        f"{', '.join(sorted(valid)) or 'none'}"))

        # -- 4. safety -------------------------------------------------
        if control in self.forbidden:
            return ValidationResult(
                False, reason=(f"control {control!r} is listed in "
                               f"safety.forbidden_controls and must not be "
                               f"sent"))

        # -- 5. scenario policy ----------------------------------------
        denied = self._policy_denial(act)
        if denied:
            return ValidationResult(False, reason=denied)

        # -- 6. ranges and durations -----------------------------------
        if act.type is ActionType.HOLD:
            duration = float(act.duration or act.seconds or 1.0)
            if duration > MAX_HOLD_SECONDS:
                corrections.append(
                    f"hold shortened from {duration:.1f}s to the "
                    f"{MAX_HOLD_SECONDS:.0f}s ceiling so no control is left "
                    f"depressed")
                duration = MAX_HOLD_SECONDS
            if duration <= 0:
                corrections.append("non-positive hold duration set to 0.5s")
                duration = 0.5
            act.duration = duration

        if act.type is ActionType.TRIGGER and act.value is not None:
            clamped = max(TRIGGER_MIN, min(TRIGGER_MAX, int(act.value)))
            if clamped != act.value:
                corrections.append(
                    f"trigger value {act.value} clamped to {clamped} "
                    f"({TRIGGER_MIN}-{TRIGGER_MAX})")
                act.value = clamped

        if act.type is ActionType.STICK:
            for axis in ("x", "y"):
                raw = getattr(act, axis)
                if raw is None:
                    continue
                clamped = max(STICK_MIN, min(STICK_MAX, int(raw)))
                if clamped != raw:
                    corrections.append(
                        f"stick {axis} {raw} clamped to {clamped}")
                    setattr(act, axis, clamped)
            if act.direction:
                allowed = {d.lower() for d in
                           self.caps.sticks.get(act.control or "", [])}
                if allowed and act.direction.lower() not in allowed:
                    # Recoverable: x/y may still carry the intent, and pad_link
                    # would reject a bad direction name anyway.
                    corrections.append(
                        f"unknown stick direction {act.direction!r} dropped "
                        f"(valid: {', '.join(sorted(allowed))})")
                    act.direction = None

        # -- 7. the one-action invariant -------------------------------
        if act.times != 1:
            corrections.append(
                f"times={act.times} forced to 1: closed-loop control observes "
                f"between presses, so a repeat count cannot be honoured")
            act.times = 1

        return ValidationResult(True, act, corrections=corrections)

    # -- scenario policy ---------------------------------------------------
    def _policy_denial(self, act: Action) -> str:
        """Enforce the scenario's `prohibited_inputs`. Empty string = allowed.

        Only ADB-flavoured bans can bite here, because a gamepad Action has no
        way to express a touch or a keystroke - which is the point: the closed
        loop's action vocabulary is gamepad-only by construction. This check
        exists so that if an ADB action type is ever added, a scenario that
        forbade it keeps being honoured without anyone having to remember.
        """
        if not self.prohibited:
            return ""
        for ban in self.prohibited:
            markers = _MODALITY_BANS.get(ban)
            if not markers:
                continue
            if ban.startswith("adb") and act.type.value.startswith("adb"):
                return (f"the scenario's controller_policy prohibits {ban!r}, "
                        f"so this action must not be sent - a test that "
                        f"reaches its goal by a banned route proves nothing "
                        f"about the route it claims to test")
        return ""

    # -- reporting ---------------------------------------------------------
    def describe_policy(self) -> str:
        bits = []
        if self.forbidden:
            bits.append("safety.forbidden_controls: "
                        + ", ".join(sorted(self.forbidden)))
        if self.prohibited:
            bits.append("scenario prohibited_inputs: "
                        + ", ".join(sorted(self.prohibited)))
        return "; ".join(bits) or "no control restrictions this run"
