"""Execute one live decision and record two-look evidence."""

from __future__ import annotations

import time

from ..logbook import log
from ..schemas import (
    Action, ActionKind, ActionType, GameState, Goal, Observation, PlanStep,
    StepResult,
)
from ..state import GraphState
from .base import Agent
from .executor import ExecutorAgent


SEARCH_TEXT_SENTINEL = "__adb_text__"


class ActorAgent(Agent):
    """Execute one validated action, then observe the result."""

    name = "actor"

    def __init__(self, ctx: object) -> None:
        super().__init__(ctx)  # type: ignore[arg-type]
        self._exec = ExecutorAgent(ctx)  # type: ignore[arg-type]

    def run(self, state: GraphState) -> GraphState:
        action: Action | None = state.get("pending_action")
        before: GameState | None = state.get("game_state")
        goal: Goal | None = state.get("goal")
        iteration = int(state.get("iteration", 0)) + 1

        if action is None:
            return {"halt_reason": "actor ran without a pending action",
                    "agent_trace": [self.trace("act", "no pending action")]}

        if self.ctx.out_of_time():
            return {"halt_reason": "safety.max_run_seconds exceeded",
                    "agent_trace": [self.trace("act", "time budget exhausted")]}

        max_iterations = int(self.s.get("execution.max_iterations", 40))
        if iteration > max_iterations:
            return {"halt_reason": f"execution.max_iterations ({max_iterations}) reached",
                    "agent_trace": [self.trace("act", "iteration limit reached")]}

        validation = self.ctx.validator.validate(
            action, before.screen_type if before else None)
        if not validation.ok:
            log.warn(f"action REJECTED by validator: {validation.reason}", indent=1)
            return {
                "iteration": iteration,
                "pending_action": None,
                "adaptations": [f"action {action.describe()} rejected: {validation.reason}"],
                "agent_trace": [self.trace("act", f"rejected: {validation.reason}")],
            }

        action = validation.action or action

        # Search text is intentionally represented as an OBSERVE sentinel in
        # Action because the live decision schema is controller-centric. Convert
        # it here, immediately before dispatch, into the existing ADB_TEXT step.
        # This is the ONLY place in the closed loop where the sentinel is legal.
        search_text_step = (
            action.type is ActionType.OBSERVE and
            action.control == SEARCH_TEXT_SENTINEL
        )
        if search_text_step:
            text = str(goal.target if goal and goal.target else "").strip()
            if not text:
                return {
                    "iteration": iteration,
                    "pending_action": None,
                    "adaptations": ["search text sentinel had no goal target"],
                    "agent_trace": [self.trace("act", "search text missing")],
                }
            step = PlanStep(
                id=f"iter{iteration:02d}",
                kind=ActionKind.ADB_TEXT,
                target=text,
                intent=(f"type {text!r} into the xCloud search field; "
                        "this is an ADB fixture, not controller evidence"),
                expectation=(f"the xCloud search UI accepts {text!r} and "
                             "the requested title becomes visible in the search UI/results"),
                observe_after=True,
            )
        else:
            step = action.to_plan_step(f"iter{iteration:02d}")

        started = time.time()
        waited_before = self.ctx.timing.total_waited
        result = StepResult(
            step=step,
            action=action,
            iteration=iteration,
            game_state_before=before,
            recovery_attempt=int(state.get("recovery_attempts", 0)),
        )

        profile = self.ctx.timing.profile_for(step.kind)
        result.settle_profile = profile.describe()
        log.step(f"[iteration {iteration}/{max_iterations}] {action.describe()}")
        log.act(f"because: {action.rationale}")
        if action.expected_states:
            log.act("may legitimately produce: " +
                    ", ".join(s.value for s in action.expected_states))

        if self.s.get("logs.logcat_enabled", True) and self.ctx.android:
            self.ctx.android.clear_logcat()

        result.dispatched, result.hardware_ok, detail = self._exec._dispatch(step)
        if detail:
            result.reasoning = detail
        (log.ok if result.hardware_ok else log.warn)(
            f"dispatched {step.kind.value} {step.target or action.control or ''} -> "
            f"hardware_ok={result.hardware_ok}" +
            (f" | {detail.splitlines()[0][:110]}" if detail else ""), indent=1)

        glance: Observation | None = None
        if self._exec._should_glance(step):
            self.ctx.timing.glance(profile)
            glance = self._exec._look(state, step, profile, phase="glance")
            result.glance_observation = glance

        if step.kind.value != "wait":
            self.ctx.timing.settle(profile)
        settled = self._exec._look(state, step, profile, phase="settle")
        settled.pad_state = self.ctx.pad.state()
        result.observation = settled
        result.reacted_on = self._exec._reaction_phase(glance, settled)
        result.waited_seconds = round(self.ctx.timing.total_waited - waited_before, 3)
        result.duration_seconds = round(time.time() - started, 3)

        after = self.ctx.state_builder.build(settled, goal=goal, previous=before)
        result.game_state_after = after
        log.see(f"now: {after.summary()}")
        log.act(f"iteration took {result.duration_seconds:.1f}s, of which "
                f"{result.waited_seconds:.1f}s was deliberate waiting")

        return {
            "iteration": iteration,
            "pending_action": None,
            "step_results": [result],
            "previous_game_state": before,
            "game_state": after,
            "agent_trace": [self.trace(
                "act",
                f"{action.describe()} -> hardware_ok={result.hardware_ok} "
                f"reacted_on={result.reacted_on} state={after.screen_type.value} "
                f"waited={result.waited_seconds}s",
                step_id=step.id)],
        }
