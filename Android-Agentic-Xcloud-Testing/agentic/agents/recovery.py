"""
recovery.py - the cheap fix, tried before the expensive diagnosis.

WHY THIS SITS BETWEEN `verify` AND `rca`
----------------------------------------
Before the closed loop there was exactly one response to a failed step:

    execute -> rca -> replan -> plan

Every failure, however trivial, cost a root-cause analysis with the strongest
configured model plus a full LLM replan of the entire remaining run. A loading
screen that needed three more seconds was charged the same price as a wiring
fault. In a run that spent 517 seconds on 10 steps and 32 LLM calls, that is a
large part of where the time went.

Most failures in UI automation are not diagnoses, they are patience:

    STILL_LOADING / UI_NOT_READY -> wait for the screen to change, then look
    OVERLAY / DIALOG_PRESENT     -> dismiss it with B
    FOCUS_WRONG                  -> re-observe; the decision agent re-navigates
    VISION_UNCERTAIN / UNKNOWN   -> look again, escalating perception

None of those needs a model. This agent handles them in code, and RCA is
reserved for what it is actually good at: attributing a real fault to a LAYER.

`poll_until` FINALLY GETS USED
-----------------------------
`timing.poll_until()` has existed since the timing module was written, with a
docstring explaining that it is strictly better than a fixed sleep because it
returns as soon as the condition holds and reports a TIMEOUT distinctly rather
than continuing as though the wait had succeeded. Nothing ever called it. The
loading recovery is exactly the case it was written for.

THE BUDGET IS A HARD STOP
-------------------------
`max_recovery_attempts` (default 2) is counted per goal, in code. Beyond it,
this agent refuses and routes to RCA. Retrying indefinitely is how a rig
produces a hundred identical failures instead of one useful diagnosis - and the
guide's own rule is attempt, verify, recover, attempt, verify, then stop.
"""

from __future__ import annotations

from ..logbook import log
from ..schemas import (Action, ActionType, Capabilities, FailureClass,
                       GameState, RECOVERABLE_FAILURES, ScreenType, Transition)
from ..state import GraphState
from .base import Agent


class RecoveryAgent(Agent):
    name = "recovery"

    def run(self, state: GraphState) -> GraphState:
        transition: Transition | None = state.get("last_transition")
        game_state: GameState | None = state.get("game_state")
        caps: Capabilities | None = state.get("capabilities")
        attempts = int(state.get("recovery_attempts", 0))
        budget = int(self.s.get(
            "execution.closed_loop.max_recovery_attempts", 2))

        if transition is None:
            return {
                "needs_rca": True,
                "agent_trace": [self.trace(
                    "recover", "no transition to recover from")],
            }

        failure = transition.failure_class

        # -- 1. is this even recoverable? -------------------------------
        if failure not in RECOVERABLE_FAILURES:
            log.warn(f"{failure.value} is not a recoverable class - routing to "
                     f"root-cause analysis rather than retrying in hope",
                     indent=1)
            return {
                "needs_rca": True,
                "adaptations": [
                    f"no cheap recovery exists for {failure.value}; "
                    f"diagnosing instead of retrying"],
                "agent_trace": [self.trace(
                    "recover", f"{failure.value} not recoverable -> rca")],
            }

        # -- 2. budget --------------------------------------------------
        if attempts >= budget:
            log.warn(f"recovery budget exhausted ({attempts}/{budget}) - "
                     f"routing to root-cause analysis", indent=1)
            return {
                "needs_rca": True,
                "adaptations": [
                    f"recovery budget of {budget} attempts exhausted while "
                    f"handling {failure.value}; a repeated failure is a "
                    f"finding, not something to keep retrying"],
                "agent_trace": [self.trace(
                    "recover", f"budget {attempts}/{budget} exhausted -> rca")],
            }

        attempt = attempts + 1

        # -- 3. the strategies ------------------------------------------
        strategy, action, note = self._strategy(failure, game_state, caps)
        log.act(f"recovery {attempt}/{budget}: {strategy} - {note}", indent=1)

        result: GraphState = {
            "recovery_attempts": attempt,
            "needs_rca": False,
            "adaptations": [f"recovery {attempt}/{budget} for "
                            f"{failure.value}: {note}"],
            "agent_trace": [self.trace(
                "recover",
                f"{failure.value} -> {strategy} (attempt {attempt}/{budget})")],
        }

        if action is not None:
            # Hand the decision agent's slot a pre-made action so the next pass
            # executes the fix directly instead of re-deciding and possibly
            # choosing the same thing that just failed.
            result["pending_action"] = action
        return result

    # ------------------------------------------------------------------
    def _strategy(self, failure: FailureClass, gs: GameState | None,
                  caps: Capabilities | None
                  ) -> tuple[str, Action | None, str]:
        """Pick the cheap fix. Returns (name, action-or-None, explanation)."""
        buttons = {b.lower() for b in (caps.buttons if caps else [])}
        timing = (caps.timing if caps else {}) or {}

        # -- still loading: poll, do not press ---------------------------
        if failure in (FailureClass.STILL_LOADING, FailureClass.UI_NOT_READY):
            timeout = float(timing.get("stream_start_wait", 20.0))
            interval = float(timing.get("loading_observation_interval", 3.0))
            changed = self._poll_for_change(timeout, interval)
            note = (f"waited up to {timeout:.0f}s for the screen to move on "
                    f"({'it changed' if changed else 'it did NOT change'})")
            # Deliberately no button press. The screen was busy; the only
            # correct action was to let it finish, and then look.
            return "wait for the UI to become ready", Action(
                type=ActionType.OBSERVE,
                rationale="re-observe after waiting for the UI to settle",
            ), note

        # -- an overlay is eating the input -----------------------------
        if failure in (FailureClass.OVERLAY_PRESENT,
                       FailureClass.DIALOG_PRESENT):
            if "b" in buttons:
                return "dismiss the overlay with B", Action(
                    type=ActionType.PRESS, control="b",
                    rationale=("an overlay or dialog was consuming the input "
                               "intended for the screen behind it"),
                    expected_states=[ScreenType.XCLOUD_HOME,
                                     ScreenType.GAME_FOCUSED,
                                     ScreenType.GAME_DETAIL,
                                     ScreenType.LIVE_GAME_STREAM],
                ), ("pressing B to clear what is stealing gamepad input")
            return "observe the overlay", Action(
                type=ActionType.OBSERVE,
                rationale="an overlay is present but no B button exists to "
                          "dismiss it, so look again and re-decide",
            ), "no B button on this pad; re-observing instead"

        # -- the highlight is on the wrong thing ------------------------
        if failure is FailureClass.FOCUS_WRONG:
            return "re-observe and re-navigate", Action(
                type=ActionType.OBSERVE,
                rationale=("the highlight was not where it was assumed to be, "
                           "so re-read the screen and let the next decision "
                           "navigate from where it actually is"),
            ), ("focus was wrong; re-reading the screen rather than pressing "
                "again from a false assumption")

        # -- the page has stopped seeing the pad ------------------------
        #
        # INPUT_IGNORED with the firmware reporting OK is the exact signature of
        # a page that has lost the gamepad: the board queued the report and
        # nothing moved. Before blaming the wiring, re-run the handshake - the
        # W3C Gamepad API drops a pad the page has not heard from, and a reloaded
        # tab or a stream handoff both cause it.
        #
        # This is the cheapest possible answer to what would otherwise be
        # diagnosed as a hardware fault, and it is why CONTROLLER_NOT_DETECTED
        # is worth having as a distinct class.
        if failure in (FailureClass.INPUT_IGNORED,
                       FailureClass.CONTROLLER_NOT_DETECTED):
            if "signal_handshake" in (caps.special_actions if caps else {}):
                return "re-run the signal handshake", Action(
                    type=ActionType.MACRO, control="signal_handshake",
                    rationale=("the input was accepted by the firmware but the "
                               "page did not react, which is what a page that "
                               "has lost track of the gamepad looks like - "
                               "announce the pad again before concluding the "
                               "hardware is at fault"),
                    expected_states=[ScreenType.XCLOUD_HOME,
                                     ScreenType.GAME_FOCUSED,
                                     ScreenType.OVERLAY,
                                     ScreenType.LIVE_GAME_STREAM],
                ), ("re-announcing the pad to the page; a browser hides a "
                    "gamepad it has not heard from")
            if "guide" in buttons:
                return "announce the pad with Guide", Action(
                    type=ActionType.PRESS, control="guide",
                    rationale=("the page appears not to be receiving input; "
                               "Guide is the most visible way to announce the "
                               "pad"),
                    expected_states=[ScreenType.OVERLAY,
                                     ScreenType.XCLOUD_HOME],
                ), "no handshake macro defined; pressing Guide instead"

        # -- we cannot see properly -------------------------------------
        if failure in (FailureClass.VISION_UNCERTAIN,
                       FailureClass.STATE_UNKNOWN):

            # A short settle first: a mid-animation frame is a common cause of
            # an unclassifiable screen, and half a second often resolves it for
            # free. The state builder will escalate to the vision LLM on the
            # next pass because the confidence will still be low.
            self.ctx.timing.sleep(
                0.8, "recovery: brief settle before re-reading a screen that "
                     "could not be classified - a mid-animation frame is a "
                     "common and self-curing cause")
            return "look again with stronger perception", Action(
                type=ActionType.OBSERVE,
                rationale=("the screen could not be classified confidently; "
                           "observing again will escalate to the vision model"),
            ), "re-observing; perception will escalate on the retry"

        # Should be unreachable: RECOVERABLE_FAILURES is checked by the caller.
        return "observe", Action(
            type=ActionType.OBSERVE,
            rationale="no specific recovery applies; re-observe",
        ), f"no specific strategy for {failure.value}"

    # ------------------------------------------------------------------
    def _poll_for_change(self, timeout: float, interval: float) -> bool:
        """Wait until the screen stops looking like it did. True if it changed.

        This is the call `timing.poll_until` was written for and never received.
        It matters that it returns EARLY: a loading screen that clears in two
        seconds should cost two seconds, not the whole timeout, and the previous
        design's only tool was a fixed sleep chosen for the worst case.
        """
        baseline = self.ctx.last_frame_path
        if self.ctx.vision is None or not baseline:
            self.ctx.timing.sleep(
                min(interval, timeout),
                "recovery: no frame baseline to poll against, so waiting a "
                "fixed interval instead")
            return False

        def changed() -> bool:
            path, _ = self.ctx.vision.capture(self.ctx.run_id, "recovery_poll")
            if not path:
                return False
            ratio = self.ctx.vision.diff(path, baseline)
            threshold = float(self.s.get("vision.motion_threshold", 0.01))
            return ratio is not None and ratio >= threshold

        return self.ctx.timing.poll_until(
            changed, timeout=timeout, interval=interval,
            reason="the screen to move on from a state that was not ready")
