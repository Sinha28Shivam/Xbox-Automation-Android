"""
actor.py - execute ONE validated action, then look twice.

    validate -> dispatch -> GLANCE -> settle -> observe -> hand to the verifier

This is the closed loop's execute node. It deliberately does NOT judge: the
verifier does that, from the state before and the state after. Splitting them is
what allows "the screen changed to something unexpected but legitimate" to be
classified as progress rather than as a failed prediction.

WHAT IS REUSED, AND WHY THAT MATTERS
------------------------------------
The two-look observation is NOT reimplemented here. `ExecutorAgent._look`,
`_reaction_phase`, `_dispatch` and `ctx.timing`'s glance/settle profiles are all
called as they are. That is deliberate:

  * `timing.MIN_GLANCE = 0.25` is a MEASURED floor, not a guess - xCloud adds
    60-100ms of network latency on top of UI animation, and a glance faster than
    that photographs the screen as it was BEFORE the input, manufacturing the
    exact false failure the two-look design was built to eliminate.
  * `reacted_on` is the field the silent-failure check and the glance-downgrade
    guard both key off. Recomputing it here, slightly differently, would let the
    report and the verdict disagree - which is worse than either being wrong,
    because it makes the whole record untrustworthy.

So this module owns the closed-loop bookkeeping (iteration counters, state
threading, validation) and borrows the hardware-proven observation cycle intact.
"""

from __future__ import annotations

import time

from ..logbook import log
from ..schemas import (Action, ActionType, GameState, Goal, Observation,
                       StepResult)
from ..state import GraphState
from .base import Agent
from .executor import ExecutorAgent


class ActorAgent(Agent):
    """Executes one decided Action and records the evidence."""

    name = "actor"

    def __init__(self, ctx: object) -> None:
        super().__init__(ctx)                            # type: ignore[arg-type]
        # Composition, not inheritance: we want the executor's observation
        # helpers without inheriting its plan-walking `run`, which is a
        # different control flow entirely.
        self._exec = ExecutorAgent(ctx)                   # type: ignore[arg-type]

    def run(self, state: GraphState) -> GraphState:
        action: Action | None = state.get("pending_action")
        before: GameState | None = state.get("game_state")
        goal: Goal | None = state.get("goal")
        iteration = int(state.get("iteration", 0)) + 1

        if action is None:
            return {
                "halt_reason": "the actor ran with no pending action - this is "
                               "a harness bug",
                "agent_trace": [self.trace("act", "no pending action")],
            }

        # -- budget guards ----------------------------------------------
        if self.ctx.out_of_time():
            return {
                "halt_reason": (
                    f"run exceeded safety.max_run_seconds "
                    f"({self.s.get('safety.max_run_seconds')}s) at closed-loop "
                    f"iteration {iteration}"),
                "agent_trace": [self.trace("act", "time budget exhausted")],
            }

        max_iterations = int(self.s.get("execution.max_iterations", 40))
        if iteration > max_iterations:
            return {
                "halt_reason": (
                    f"the closed loop reached execution.max_iterations "
                    f"({max_iterations}) without reaching the goal"),
                "agent_trace": [self.trace(
                    "act", f"iteration limit {max_iterations} reached")],
            }

        # -- validate ----------------------------------------------------
        #
        # The same wall the planner walks through. An action that fails here is
        # never sent, and the refusal is recorded rather than silently dropped.
        validation = self.ctx.validator.validate(
            action, before.screen_type if before else None)
        if not validation.ok:
            log.warn(f"action REJECTED by the validator: {validation.reason}",
                     indent=1)
            return {
                "iteration": iteration,
                "pending_action": None,
                "adaptations": [f"action {action.describe()} was rejected: "
                                f"{validation.reason}"],
                "agent_trace": [self.trace(
                    "act", f"rejected: {validation.reason}")],
            }

        action = validation.action or action
        for correction in validation.corrections:
            log.warn(f"validator adjusted the action: {correction}", indent=1)

        # -- DONE is not a hardware action -------------------------------
        if action.type is ActionType.DONE:
            log.ok(f"the decision agent reports the goal is reached: "
                   f"{action.rationale}", indent=1)
            return {
                "iteration": iteration,
                "pending_action": None,
                "goal_complete": True,
                "agent_trace": [self.trace("act", f"DONE - {action.rationale}")],
            }

        # -- execute -----------------------------------------------------
        step = action.to_plan_step(f"iter{iteration:02d}")
        started = time.time()
        waited_before = self.ctx.timing.total_waited

        result = StepResult(step=step, action=action, iteration=iteration,
                            game_state_before=before,
                            recovery_attempt=int(
                                state.get("recovery_attempts", 0)))

        profile = self.ctx.timing.profile_for(step.kind)
        result.settle_profile = profile.describe()

        log.step(f"[iteration {iteration}/{max_iterations}] "
                 f"{action.describe()}")
        log.act(f"because: {action.rationale}")
        if action.expected_states:
            log.act("may legitimately produce: "
                    + ", ".join(s.value for s in action.expected_states))

        if self.s.get("logs.logcat_enabled", True) and self.ctx.android:
            self.ctx.android.clear_logcat()

        result.dispatched, result.hardware_ok, detail = \
            self._exec._dispatch(step)
        if detail:
            result.reasoning = detail
        (log.ok if result.hardware_ok else log.warn)(
            f"dispatched {action.describe()} -> "
            f"hardware_ok={result.hardware_ok}"
            + (f" | {detail.splitlines()[0][:110]}" if detail else ""),
            indent=1)

        # -- the two looks, borrowed wholesale ---------------------------
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
        result.waited_seconds = round(
            self.ctx.timing.total_waited - waited_before, 3)
        result.duration_seconds = round(time.time() - started, 3)

        # -- interpret the settled frame ---------------------------------
        after = self.ctx.state_builder.build(
            settled, goal=goal, previous=before)
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
                f"reacted_on={result.reacted_on} "
                f"state={after.screen_type.value} "
                f"waited={result.waited_seconds}s",
                step_id=step.id)],
        }
