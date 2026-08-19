"""
handshake.py - make the pad VISIBLE to the browser, and prove that it is.

THE PROBLEM THIS SOLVES
=======================
Every fresh page load starts deaf. The W3C Gamepad API deliberately hides a
gamepad from a page until that pad sends a button event:

    navigator.getGamepads() returns nothing for a pad that has not yet
    reported a button press.

It is an anti-fingerprinting rule, present in every browser, and it cannot be
configured away. The consequence for this rig is a perfect trap, because three
things are simultaneously true:

    the Leonardo IS enumerated as a USB HID gamepad          correct
    the firmware DOES answer OK to every command             correct
    xCloud CANNOT see the pad at all                         also correct

So a run that launches the PWA and starts pressing buttons has every input
silently discarded - not by the board, not by the phone, but by the PAGE. The
symptom is indistinguishable from a wiring fault: `hardware_ok=True`, screen
unchanged, and a reader sent looking for a USB-OTG problem that does not exist.

WHY THIS IS A GRAPH NODE AND NOT A PLAN STEP
============================================
It is a PRECONDITION of the page, so it belongs to the page's lifecycle rather
than to any scenario. Three consequences follow, and all three were wrong before:

1. It must run AFTER the PWA launch, not before. `DeviceAgent` runs first, so a
   handshake performed there is discarded by the page load that follows. (There
   was a `_verify_guide_handshake` method in device.py that did exactly this -
   and it was never called at all, so it had never once run.)

2. It must run after EVERY launch, including a mid-run reload. `handshake_done`
   is reset by the launch node, so the loop cannot forget.

3. It must be VERIFIED, not hoped for. controls.yaml warns that `guide` is
   unverified because some Android builds intercept the HID Home usage before
   the app sees it. If that happens, a fire-and-forget handshake leaves the run
   blind and every later step looks like a hardware fault. So this agent
   watches for the overlay and escalates if it does not appear.

WHAT COUNTS AS PROOF
====================
The xCloud Guide overlay is a large, obvious visual change, so either signal is
accepted:

    frame diff  >= the motion threshold   (the overlay animated in)
    OCR         contains overlay vocabulary (settings/friends/quit/...)

Either one proves the page received a button report, which is the actual claim.
If neither appears, one more Guide press is tried - the first is often consumed
by the page taking focus rather than by the pad announcing itself - and only
then is the run halted with CONTROLLER_NOT_DETECTED, which is a real finding
worth stopping for.

Without adb there is nothing to look at, so this degrades to fire-and-forget and
SAYS SO in the report rather than quietly claiming a verified handshake.
"""

from __future__ import annotations

from ..logbook import log
from ..schemas import EnvironmentReport, FailureClass
from ..state import GraphState
from .base import Agent

# Words that appear on the xCloud Guide overlay. Generic console/overlay
# vocabulary, deliberately not tied to any game.
OVERLAY_CUES = ("guide", "settings", "friends", "quit", "sign out", "audio",
                "home", "recent", "parties", "achievements", "volume",
                "leave game", "back to")

# Words that mean the page is still ASKING for a controller - the opposite of a
# successful handshake, and a much stronger signal than an unchanged frame.
PROMPT_CUES = ("connect a controller", "controller required", "no controller",
               "press a button on your controller", "reconnect your controller")


class HandshakeAgent(Agent):
    """Runs `signal_handshake` from controls.yaml and verifies the reaction."""

    name = "handshake"

    def run(self, state: GraphState) -> GraphState:
        if not self.s.get("execution.closed_loop.handshake.enabled", True):
            return {
                "handshake_done": True,
                "agent_trace": [self.trace(
                    "handshake", "skipped: disabled in config")],
            }

        if state.get("handshake_done"):
            return {"agent_trace": [self.trace(
                "handshake", "already done for this page load")]}

        pad = self.ctx.pad
        if pad is None or pad.pad is None:
            return {
                "handshake_done": False,
                "agent_trace": [self.trace(
                    "handshake", "no pad link, nothing to hand shake")],
            }

        attempts = int(state.get("handshake_attempts", 0))
        budget = int(self.s.get(
            "execution.closed_loop.handshake.max_attempts", 2))

        log.hw("SIGNAL HANDSHAKE: making the pad visible to the browser "
               "(a fresh page cannot see a gamepad until it sends a button "
               "report)", indent=0)

        verified, detail, escalated = self._handshake(attempts)
        attempts += 1

        # Record it as environment evidence, so the report can state plainly
        # whether the browser could see the pad. A run where this silently
        # failed currently reads identically to one where xCloud ignored the
        # input, which is the same hardware_ok/app_reacted confusion this
        # project keeps having to disentangle.
        env: EnvironmentReport | None = state.get("environment")
        if env is not None:
            env.guide_signal_verified = verified
            env.guide_verification_notes = detail

        # NOTE: `detail` is folded into the trace's own detail string rather
        # than passed as a keyword. `Agent.trace(action, detail="", **extra)`
        # already has a `detail` parameter, so `trace(a, b, detail=c)` raises
        # TypeError - which crashed this agent on its first real run.
        out: GraphState = {
            "handshake_attempts": attempts,
            "agent_trace": [self.trace(
                "handshake",
                f"verified={verified} attempts={attempts}/{budget}"
                + (" escalated=3rd-press" if escalated else "")
                + f" - {detail}")],
        }


        if verified:
            log.ok(f"HANDSHAKE VERIFIED: {detail}", indent=1)
            out["handshake_done"] = True
            return out

        # Not verified. Retry while there is budget - the page may simply have
        # been mid-load when the first attempt ran.
        if attempts < budget:
            log.warn(f"handshake not confirmed ({detail}); retrying "
                     f"{attempts + 1}/{budget}", indent=1)
            out["handshake_done"] = False
            out["adaptations"] = [
                f"signal handshake attempt {attempts}/{budget} could not be "
                f"confirmed: {detail}"]
            return out

        # Out of budget. Whether this halts depends on whether we could SEE.
        if not self.ctx.vision.can_screenshot:
            # No eyes: we cannot distinguish "it worked" from "it did not", and
            # blocking a run over a check we were never able to perform would be
            # dishonest in the other direction. Proceed, clearly labelled.
            log.warn("handshake sent but NOT verifiable (no screenshots). The "
                     "run continues; if input appears to do nothing, this is "
                     "the first thing to suspect.", indent=1)
            out["handshake_done"] = True
            out["adaptations"] = [
                "the signal handshake was sent but could not be verified "
                "because no screenshots are available, so 'the browser can see "
                "the pad' is an ASSUMPTION in this run, not a finding"]
            return out

        # We could see, and we saw nothing. That is a real finding.
        log.error("HANDSHAKE FAILED: the browser never acknowledged the pad. "
                  "Every subsequent input would be discarded by the PAGE, so "
                  "the run is stopped here rather than producing a screen full "
                  "of false silent failures.", indent=1)
        out["handshake_done"] = False
        out["halt_reason"] = (
            f"the gamepad signal handshake failed after {attempts} attempts: "
            f"{detail}. The browser did not acknowledge the controller, so no "
            f"input can reach xCloud. This is {FailureClass.CONTROLLER_NOT_DETECTED.value}"
            f" - check that the Leonardo's ON LED is lit, that the OTG adapter "
            f"is at the PHONE end, and that this Android build does not "
            f"intercept the HID Home usage (controls.yaml flags `guide` as "
            f"unverified for exactly this reason).")
        out["needs_rca"] = True
        return out

    # ------------------------------------------------------------------
    def _handshake(self, prior_attempts: int) -> tuple[bool, str, bool]:
        """Send the sequence and look. Returns (verified, detail, escalated)."""
        pad = self.ctx.pad
        vision = self.ctx.vision
        timing = self.ctx.timing
        rig = (self.ctx.state_builder and {}) or {}
        caps = getattr(pad, "cfg", None)
        rig_timing = dict(getattr(caps, "timing", {}) or {}) if caps else {}

        can_see = bool(vision and vision.can_screenshot)
        before = None
        if can_see:
            before, _ = vision.capture(self.ctx.run_id,
                                       f"handshake{prior_attempts}_before")

        # -- send it -----------------------------------------------------
        #
        # Prefer the YAML-declared special action so the sequence lives with the
        # rig it belongs to: a phone needing three presses is a config edit, not
        # a code change. Fall back to explicit presses only if the action is
        # missing, so an older controls.yaml still works.
        used_special = False
        if caps is not None and "signal_handshake" in (
                getattr(caps, "special", {}) or {}):
            ok, _ = pad.dispatch(self._special_step())
            used_special = bool(ok)

        if not used_special:
            presses = int(self.s.get(
                "execution.closed_loop.handshake.guide_presses", 2))
            pad.pad.press_times("guide", presses, interval=0.35)
            timing.sleep(
                float(rig_timing.get("handshake_settle", 1.2)),
                "handshake: let the Guide overlay animate in before looking - "
                "photographing it mid-fade would report the pad as unseen")

        if not can_see:
            return (False,
                    "sent, but no screenshots are available so it could not be "
                    "verified", False)

        # -- look --------------------------------------------------------
        after, _ = vision.capture(self.ctx.run_id,
                                  f"handshake{prior_attempts}_after")
        verified, detail = self._inspect(before, after)

        escalated = False
        if not verified:
            # The first press is commonly eaten by the page taking focus rather
            # than by the pad announcing itself. One more, then give up.
            log.warn("no overlay after the handshake - trying one more Guide "
                     "press (the first is often consumed by the page taking "
                     "focus)", indent=2)
            escalated = True
            pad.pad.press("guide")
            timing.sleep(float(rig_timing.get("handshake_settle", 1.2)),
                         "handshake: settle after the escalated third press")
            after, _ = vision.capture(
                self.ctx.run_id, f"handshake{prior_attempts}_after_escalated")
            verified, detail = self._inspect(before, after)

        # -- always dismiss, verified or not -----------------------------
        #
        # Leave the UI where we found it. If the overlay DID open and we walked
        # away, the first real observation would measure our own side effect
        # instead of the screen the scenario cares about.
        pad.pad.press("b")
        timing.sleep(float(rig_timing.get("handshake_dismiss", 0.8)),
                     "handshake: B pressed to dismiss the overlay and leave the "
                     "page as it was found")

        return verified, detail, escalated

    def _special_step(self):
        """A PlanStep that runs the YAML special action through PadTool."""
        from ..schemas import ActionKind, PlanStep
        return PlanStep(
            id="signal_handshake",
            kind=ActionKind.SPECIAL,
            target="signal_handshake",
            intent="make the pad visible to the browser after a page load",
            observe_after=False,
        )

    # ------------------------------------------------------------------
    def _inspect(self, before: str | None,
                 after: str | None) -> tuple[bool, str]:
        """Did the page acknowledge the pad? Returns (verified, why).

        Two independent signals, either sufficient, because they fail in
        different circumstances: OCR can read the overlay's own words but needs
        the tesseract binary, while the frame diff needs no text but cannot say
        WHAT changed. Requiring both would make a missing optional dependency
        look like a hardware fault.
        """
        vision = self.ctx.vision
        text = (vision.ocr(after) or "").lower()

        # Checked FIRST and treated as decisive. A page still asking for a
        # controller is direct evidence the handshake did not land, and it
        # outranks any amount of pixel movement - a "connect a controller"
        # banner animating in would otherwise be read as success.
        for cue in PROMPT_CUES:
            if cue in text:
                return False, (f"the page is still asking for a controller "
                               f"(saw {cue!r}), so the pad has not been "
                               f"acknowledged")

        hits = [cue for cue in OVERLAY_CUES if cue in text]
        if hits:
            return True, (f"the Guide overlay is on screen (OCR found "
                          f"{', '.join(repr(h) for h in hits[:3])}), which "
                          f"proves the page received a button report")

        ratio = vision.diff(after, before)
        threshold = float(self.s.get("vision.motion_threshold", 0.01))
        if ratio is not None and ratio >= threshold:
            return True, (f"the screen changed by {ratio:.2%} (threshold "
                          f"{threshold:.2%}) in response to the Guide presses, "
                          f"so the page is receiving gamepad input")

        if ratio is None:
            return False, ("no frame comparison was possible and OCR found no "
                           "overlay text, so the handshake is unverified")

        return False, (f"the screen moved only {ratio:.2%} and no overlay text "
                       f"was found: the page does not appear to have seen the "
                       f"pad")
