"""
verifier.py - was that a valid transition toward the goal?

    state_before + action + state_after  ->  SUCCESS / INTERMEDIATE
                                             FAILURE / UNKNOWN

THE QUESTION THIS ASKS, AND WHY THE OLD ONE WAS WRONG
-----------------------------------------------------
The previous judge asked: "does the screen look like what the step predicted?"
That question cannot be answered correctly whenever more than one next screen is
legitimate, and on xCloud that is the normal case. Selecting a game tile may
open a detail page, or hand straight off to fullscreen and start loading. Both
are correct. A rig that admits only the first will report a successful launch as
a failure, run a full root-cause analysis on it, and then "recover" by pressing
A again - on a screen that already accepted the first press.

So the question is now: "given where we were, what we sent, and where we are
now, was that valid progress?" `INTERMEDIATE` is the answer that was previously
unrepresentable, and it is the whole point.

THE PRECEDENCE ORDER IS FIXED IN CODE
-------------------------------------
This module is where the project's hard-won safeguards continue to live, and
they outrank the classifier. In order:

  1. the model (or the mechanical table) proposes a classification
  2. NOTHING MOVED + firmware said OK  -> forced FAILURE(INPUT_IGNORED), and
     `silent_failure` is set. This is the parent README's documented trap
     ("commands say ok but phone does nothing") and no amount of model optimism
     outranks two still frames.
  3. proposed FAILURE, but the GLANCE moved -> downgraded to UNKNOWN. The
     settled frame supporting a FALSE verdict while the glance clearly moved
     means the judgement is about the WRONG MOMENT. "We looked too late" is a
     different finding from "the input never arrived", and conflating them is
     what once sent a reader hunting a USB-OTG fault that did not exist.
  4. the evaluator's verdict ceiling may still lower the run's overall verdict.

Pixels outrank prose; code outranks the model.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..logbook import log
from ..schemas import (Action, FailureClass, GameState, Goal, ScreenType,
                       StepResult, Transition, TransitionClass, WAITING_STATES)
from ..state import GraphState
from .base import Agent

ROLE = """\
You classify ONE state transition in a gamepad automation run.

You are given: the state before, the action sent, the state after, and the states
that action could legitimately have produced.

Reply with:
  classification : success | intermediate | failure | unknown
  transition_valid : true/false
  failure_class  : one of the listed failure classes, or none
  next_recommendation : WAIT | OBSERVE | CONTINUE | RECOVER | STOP
  confidence     : 0.0-1.0
  reasoning      : one or two sentences citing the STATES, not intuition

Rules that matter more than being agreeable:
* INTERMEDIATE is the right answer for valid progress that is not yet the goal.
  A loading screen, a fullscreen handoff, a game splash or a connecting screen
  after a launch action are all INTERMEDIATE with next_recommendation=WAIT.
  They are NOT failures, and calling them failures wastes the run.
* SUCCESS means the goal state was reached, or the action achieved exactly what
  it set out to achieve.
* FAILURE means the state moved somewhere the action could not legitimately
  lead, or nothing happened at all when something should have.
* UNKNOWN is correct and expected when the evidence cannot settle it. Never turn
  UNKNOWN into SUCCESS because the run "seemed to be going well".
* An unchanged screen after a navigation press is NOT automatically a failure:
  the highlight may have been at the end of a row. Say so, and prefer UNKNOWN
  over FAILURE unless nothing moved at all.
* A WAIT action that leaves the state unchanged while still loading is
  INTERMEDIATE, not a failure. Loading takes as long as it takes.
"""


class _Judgement(BaseModel):
    """Local schema - only this agent needs it."""
    classification: TransitionClass = TransitionClass.UNKNOWN
    transition_valid: bool = False
    failure_class: FailureClass = FailureClass.NONE
    next_recommendation: str = ""
    confidence: float = 0.0
    reasoning: str = ""


class VerifierAgent(Agent):
    name = "verifier"

    def run(self, state: GraphState) -> GraphState:
        results: list[StepResult] = list(state.get("step_results", []))
        goal: Goal | None = state.get("goal")
        before: GameState | None = state.get("previous_game_state")
        after: GameState | None = state.get("game_state")

        if not results or goal is None or after is None:
            return {"agent_trace": [
                self.trace("verify", "nothing to verify yet")]}

        result = results[-1]
        action = result.action
        transition = self._classify(result, action, before, after, goal)

        # Mirror the classification onto the legacy fields so the reporter, the
        # RCA agent and the evaluator keep working untouched.
        result.transition = transition
        result.silent_failure = transition.silent_failure
        result.expectation_met = {
            TransitionClass.SUCCESS: True,
            TransitionClass.INTERMEDIATE: True,
            TransitionClass.FAILURE: False,
            TransitionClass.UNKNOWN: None,
        }[transition.classification]
        result.confidence = transition.confidence
        result.reasoning = transition.reasoning

        emit = {TransitionClass.SUCCESS: log.ok,
                TransitionClass.INTERMEDIATE: log.act,
                TransitionClass.FAILURE: log.error,
                TransitionClass.UNKNOWN: log.warn}[transition.classification]
        emit(f"{transition.describe()} (confidence "
             f"{transition.confidence:.0%}) - {transition.reasoning[:150]}",
             indent=1)
        if transition.silent_failure:
            log.error("SILENT FAILURE: the firmware accepted the command and "
                      "NEITHER the glance nor the settled frame moved", indent=2)

        return {
            "transitions": [transition],
            "last_transition": transition,
            "goal_complete": transition.goal_complete,
            "agent_trace": [self.trace(
                "verify",
                f"{transition.describe()} valid={transition.transition_valid} "
                f"failure_class={transition.failure_class.value} "
                f"next={transition.next_recommendation}")],
        }

    # ------------------------------------------------------------------
    def _classify(self, result: StepResult, action: Action | None,
                  before: GameState | None, after: GameState,
                  goal: Goal) -> Transition:
        expected = list(action.expected_states) if action else []
        # The scenario's own allowed_transitions widen what counts as valid, so
        # the YAML's state model is authoritative rather than decorative.
        if before is not None:
            expected += goal.allowed_transitions.get(
                before.screen_type.value, [])
        expected += goal.intermediate_states

        transition = Transition(
            state_before=before.screen_type if before else ScreenType.UNKNOWN,
            state_after=after.screen_type,
            action=action,
            expected=list(dict.fromkeys(expected)),
        )

        moved = result.reacted_on in ("glance", "settle", "both")
        nothing_moved = result.reacted_on == "neither"
        acted = bool(action and action.type.value in (
            "press", "hold", "stick", "trigger", "macro"))

        # -- 0. the command itself failed -------------------------------
        if not result.hardware_ok and result.dispatched:
            transition.classification = TransitionClass.FAILURE
            transition.failure_class = FailureClass.INPUT_IGNORED
            transition.confidence = 0.9
            transition.next_recommendation = "RECOVER"
            transition.reasoning = (
                "the firmware did not accept the command, so no claim about "
                "the application can follow from it")
            return transition

        # -- 1. the goal ------------------------------------------------
        if goal.is_success(after):
            transition.classification = TransitionClass.SUCCESS
            transition.transition_valid = True
            transition.goal_complete = True
            transition.confidence = after.confidence
            transition.next_recommendation = "STOP"
            transition.reasoning = (
                f"the goal state {after.screen_type.value} is on screen "
                f"(perception confidence {after.confidence:.0%})")
            transition.evidence = list(after.evidence[:4])
            return transition

        # -- 2. terminal failure ----------------------------------------
        if goal.is_failure(after) or after.is_fatal():
            transition.classification = TransitionClass.FAILURE
            transition.failure_class = (
                FailureClass.SESSION_EXPIRED
                if after.screen_type is ScreenType.SESSION_EXPIRED
                else FailureClass.STREAM_ERROR)
            transition.confidence = after.confidence
            transition.next_recommendation = "STOP"
            transition.reasoning = (
                f"{after.screen_type.value} is a terminal failure state for "
                f"this goal; no further controller action can recover it")
            transition.evidence = list(after.evidence[:4])
            return transition

        # -- 3. a valid intermediate ------------------------------------
        #
        # Checked BEFORE the model is consulted, and before any
        # "nothing moved" reasoning. A loading screen is the single most
        # common legitimate outcome of a launch action, and the run that
        # motivated this whole change failed precisely here.
        if after.screen_type in WAITING_STATES or after.loading:
            transition.classification = TransitionClass.INTERMEDIATE
            transition.transition_valid = True
            transition.failure_class = FailureClass.NONE
            transition.confidence = max(after.confidence, 0.7)
            transition.next_recommendation = "WAIT"
            transition.reasoning = (
                f"{after.screen_type.value} is valid intermediate progress, "
                f"not a failure: the application is working through a "
                f"transition and the correct response is to wait and look "
                f"again, NOT to send the action again")
            transition.evidence = list(after.evidence[:4])
            return transition

        # -- 4. silent failure: nothing moved at all --------------------
        #
        # PRESERVED from the pre-closed-loop executor, with the same
        # strengthened bar: BOTH looks must be still. Under a single-look rule
        # this fired on steps whose evidence had merely been photographed too
        # late, which is a harness defect wearing the costume of a hardware
        # fault - the most expensive kind of wrong answer.
        if (acted and nothing_moved
                and not self.s.get("hardware.dry_run", False)):
            transition.classification = TransitionClass.FAILURE
            transition.failure_class = FailureClass.INPUT_IGNORED
            transition.silent_failure = bool(result.hardware_ok)
            transition.confidence = 0.8
            transition.next_recommendation = "RECOVER"
            transition.reasoning = (
                "the firmware accepted the command and NEITHER the glance nor "
                "the settled frame moved past the motion threshold, so the "
                "application did not react to this input at either moment")
            transition.evidence = [
                f"reacted_on={result.reacted_on}",
                f"glance ratio={self._ratio(result.glance_observation)}",
                f"settled ratio={self._ratio(result.observation)}",
            ]
            return transition

        # -- 5. ask the model for the genuinely open cases --------------
        judgement = self.think(
            _Judgement, self.system_prompt(ROLE),
            self._evidence(result, action, before, after, goal, transition),
            default=None)

        if judgement is not None:
            transition.classification = judgement.classification
            transition.transition_valid = judgement.transition_valid
            transition.failure_class = judgement.failure_class
            transition.next_recommendation = (
                judgement.next_recommendation or "CONTINUE")
            transition.confidence = judgement.confidence
            transition.reasoning = judgement.reasoning
        else:
            self._mechanical(transition, result, after, moved, nothing_moved,
                             acted)

        # -- 6. the glance guard, PRESERVED -----------------------------
        #
        # The inverse of the silent-failure check and the reason a whole class
        # of false negatives disappeared. If the verdict is FAILURE but the
        # glance clearly moved, the judgement is about the wrong moment.
        if (transition.classification is TransitionClass.FAILURE
                and result.reacted_on == "glance"
                and not transition.silent_failure):
            transition.classification = TransitionClass.UNKNOWN
            transition.confidence = min(transition.confidence, 0.4)
            transition.reasoning += (
                f" DOWNGRADED to unknown: the settled frame supports a failure "
                f"verdict, but the glance taken moments earlier DID move "
                f"({self._ratio(result.glance_observation)}). The input reached "
                f"the application and the reaction had settled by the time the "
                f"judged frame was taken, so this is a question of WHEN we "
                f"looked, not of whether the app responded.")

        # -- 7. goal completion is decided in code ----------------------
        #
        # Never taken from the model. A classifier that could declare the run
        # complete would be able to end it by being agreeable, which is exactly
        # the failure mode the verdict ceiling exists to prevent.
        transition.goal_complete = goal.is_success(after)

        if transition.state_after in transition.expected:
            transition.transition_valid = True

        return transition

    # ------------------------------------------------------------------
    def _mechanical(self, transition: Transition, result: StepResult,
                    after: GameState, moved: bool, nothing_moved: bool,
                    acted: bool) -> None:
        """No-LLM classification. Weaker, never absent, always labelled."""
        if after.screen_type in transition.expected:
            transition.classification = TransitionClass.INTERMEDIATE
            transition.transition_valid = True
            transition.confidence = 0.5
            transition.next_recommendation = "CONTINUE"
            transition.reasoning = (
                f"no LLM available. Mechanically: {after.screen_type.value} is "
                f"among the states this action could legitimately produce, so "
                f"this is valid progress.")
            return

        if after.screen_type is ScreenType.UNKNOWN:
            transition.classification = TransitionClass.UNKNOWN
            transition.failure_class = FailureClass.STATE_UNKNOWN
            transition.confidence = 0.2
            transition.next_recommendation = "OBSERVE"
            transition.reasoning = (
                "no LLM available and the screen could not be classified, so "
                "this transition is unjudged - look again.")
            return

        if acted and moved:
            transition.classification = TransitionClass.UNKNOWN
            transition.confidence = 0.3
            transition.next_recommendation = "OBSERVE"
            transition.reasoning = (
                f"no LLM available. The screen DID change and is now "
                f"{after.screen_type.value}, which was not among the expected "
                f"states, but whether that is progress cannot be determined "
                f"mechanically.")
            return

        transition.classification = TransitionClass.UNKNOWN
        transition.confidence = 0.2
        transition.next_recommendation = "OBSERVE"
        transition.reasoning = (
            "no LLM available and no mechanical rule applies to this "
            "transition; it is unjudged.")

    # ------------------------------------------------------------------
    def _evidence(self, result: StepResult, action: Action | None,
                  before: GameState | None, after: GameState, goal: Goal,
                  transition: Transition) -> str:
        lines = [
            "THE GOAL",
            f"  {goal.description or 'reach the target state'}",
            "  success states: " + (", ".join(
                s.value for s in goal.success_states) or "unspecified"),
            "",
            "STATE BEFORE",
            f"  {before.summary() if before else 'not recorded'}",
            "",
            "ACTION SENT",
            f"  {action.describe() if action else 'none'}",
            f"  why: {action.rationale if action else 'not stated'}",
            f"  firmware accepted it (hardware_ok): {result.hardware_ok}",
            f"  waits applied: {result.settle_profile}",
            "",
            "STATE AFTER",
            f"  {after.summary()}",
        ]
        if after.visible_text:
            lines.append("  text on screen: "
                         + " | ".join(after.visible_text[:12]))
        if after.evidence:
            lines.append("  why it was classified this way:")
            lines += [f"    - {e}" for e in after.evidence[:6]]

        lines += [
            "",
            "STATES THIS ACTION COULD LEGITIMATELY HAVE PRODUCED",
            "  " + (", ".join(s.value for s in transition.expected)
                    or "none were declared"),
            "",
            "MOTION EVIDENCE (two looks per action)",
            f"  reacted_on = {result.reacted_on}",
            f"  glance ratio  = {self._ratio(result.glance_observation)}",
            f"  settled ratio = {self._ratio(result.observation)}",
            "  A cloud UI animates: a highlight can move and settle within a "
            "second. If the glance moved and the settled frame did not, the "
            "input DID arrive. Only 'neither' means nothing happened.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _ratio(obs: object) -> str:
        ratio = getattr(obs, "change_ratio", None) if obs is not None else None
        if obs is None:
            return "not taken"
        if ratio is None:
            return "not measured"
        return f"{ratio:.2%}"
