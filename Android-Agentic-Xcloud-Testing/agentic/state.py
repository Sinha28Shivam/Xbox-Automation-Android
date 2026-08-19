"""
state.py - the single object every LangGraph node reads and writes.

LangGraph merges each node's returned dict into this TypedDict. The rules that
keep that safe:

* A node returns ONLY the keys it changed. Returning the whole state makes
  concurrent branches overwrite each other's work.
* `step_results` and `agent_trace` are append-only, using the `operator.add`
  reducer, so an executor node adding a result never clobbers the trace.
* Everything else is last-write-wins, which is fine because exactly one node
  owns each of those keys (device -> environment, planner -> plan, ...).

`RunContext` holds the things that must NOT be serialised into the state: the
open serial port, the LLM factory, the settings. LangGraph checkpointers try to
pickle state, and a live pyserial handle cannot be pickled - and must not be
duplicated anyway, because only one process may hold the port.
"""

from __future__ import annotations

import operator
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from .llm import LLMFactory
from .schemas import (Action, Capabilities, EnvironmentReport, Evaluation,
                      GameState, Goal, Observation, RootCauseAnalysis,
                      ScenarioSpec, StepResult, TestPlan, TestReport,
                      Transition, Verdict)
from .settings import Settings



# ==========================================================================
# Non-serialisable run context
# ==========================================================================
@dataclass
class RunContext:
    """Live resources, passed to nodes as a closure - never inside the state."""
    settings: Settings
    llm: LLMFactory
    pad: Any = None            # tools.pad.PadTool
    android: Any = None        # tools.android.AndroidTool
    vision: Any = None         # tools.vision.VisionTool
    # timing.Timing - the ONE place the project sleeps. Lives here rather than
    # being constructed per agent so `total_waited` is a single run-wide number
    # the report can quote, and `execution.settle.scale` applies everywhere at
    # once. Defaulted lazily in `__post_init__` so older callers still work.
    timing: Any = None         # timing.Timing
    # perception.StateBuilder - Observation -> GameState. One instance per run so
    # its fast/escalated counters describe the whole run, which is how the report
    # can show that the cheap perception tier is actually saving vision calls
    # rather than merely existing.
    state_builder: Any = None
    # control.ActionValidator - the single fence both the planner and the
    # decision agent pass through. Constructed once capabilities are known, so
    # it is filled in by the device node rather than at build time.
    validator: Any = None
    run_id: str = ""
    started_at: float = field(default_factory=time.time)
    artifacts: list[str] = field(default_factory=list)
    # Kept so ObserverAgent can diff the current frame against the last one.
    last_frame_path: str | None = None

    def __post_init__(self) -> None:
        if self.timing is None:
            # Imported here, not at module scope: timing imports schemas, and
            # state imports schemas too, so a top-level import would be a cycle.
            from .timing import Timing
            self.timing = Timing(self.settings)
        if self.state_builder is None:
            from .perception import StateBuilder
            self.state_builder = StateBuilder(self.settings, self.vision,
                                              self.llm)
        if self.validator is None:
            # Built with empty capabilities and REPLACED by the device node once
            # controls.yaml has been read. Defaulting to empty is the safe
            # direction: an empty capability list rejects every control, so a
            # wiring bug that skipped discovery fails loudly instead of quietly
            # sending buttons the rig may not have.
            from .control import ActionValidator
            self.validator = ActionValidator(self.settings)


    def elapsed(self) -> float:
        return time.time() - self.started_at


    def out_of_time(self) -> bool:
        budget = float(self.settings.get("safety.max_run_seconds", 900))
        return budget > 0 and self.elapsed() > budget


# ==========================================================================
# Graph state
# ==========================================================================
class GraphState(TypedDict, total=False):
    """Total=False so every node can return a partial dict."""

    # -- input ------------------------------------------------------------
    run_id: str
    raw_scenario: str            # whatever the user gave us, verbatim
    scenario_source: str         # file path or "<cli>"

    # -- agent outputs ----------------------------------------------------
    environment: EnvironmentReport
    capabilities: Capabilities
    scenario: ScenarioSpec
    plan: TestPlan
    step_results: Annotated[list[StepResult], operator.add]
    baseline: Observation        # the screen before we touched anything
    evaluation: Evaluation
    root_cause: RootCauseAnalysis
    report: TestReport

    # -- closed-loop state ------------------------------------------------
    # What the run is trying to reach, in STATES rather than keystrokes. Parsed
    # from the scenario once, then read by the decision agent and the verifier.
    goal: Goal
    # The world as it is now, and as it was before the last action. Two keys
    # rather than a history lookup because the verifier's whole question is
    # "before + action -> after", and reconstructing `before` from a list is
    # how an off-by-one silently compares the wrong pair of screens.
    game_state: GameState
    previous_game_state: GameState | None
    # The action chosen but not yet executed. A separate key so the recovery
    # agent can pre-empt the decision agent by filling it directly - a recovery
    # that had to ask the decider for its fix could be handed back the same
    # action that just failed.
    pending_action: Action | None
    # Append-only, like step_results: the decision agent reads the last few to
    # avoid repeating an action that did nothing.
    transitions: Annotated[list[Transition], operator.add]
    last_transition: Transition | None
    goal_complete: bool
    iteration: int
    recovery_attempts: int

    # -- browser handshake ------------------------------------------------
    # Has this PAGE acknowledged the gamepad yet?
    #
    # The W3C Gamepad API hides a pad from a page until the pad sends a button
    # event, so a freshly loaded page cannot see the controller however well the
    # hardware is wired. `handshake_done` tracks whether that has been done for
    # the CURRENT page.
    #
    # The launcher sets it False on every launch. That is what makes the
    # handshake automatic after each page load rather than something a human has
    # to remember - and it covers a mid-run reload, not just startup.
    handshake_done: bool
    handshake_attempts: int


    # -- control flow -----------------------------------------------------
    cursor: int                  # index of the next step to execute (plan mode)
    replans: int
    verdict: Verdict
    halt_reason: str | None
    # Set by the executor when a step's expectation fails; the router reads it
    # to decide execute -> rca -> replan instead of running the rest of a plan
    # that is already off the rails.
    needs_rca: bool
    # Free-form notes the reactive path uses to explain a deviation.
    adaptations: Annotated[list[str], operator.add]
    agent_trace: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]


def new_state(raw_scenario: str, source: str = "<cli>",
              run_id: str | None = None) -> GraphState:
    rid = run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    return {
        "run_id": rid,
        "raw_scenario": raw_scenario,
        "scenario_source": source,
        "step_results": [],
        "cursor": 0,
        "replans": 0,
        "verdict": Verdict.INCONCLUSIVE,
        "halt_reason": None,
        "needs_rca": False,
        "adaptations": [],
        "agent_trace": [],
        "errors": [],
        # closed loop
        "transitions": [],
        "previous_game_state": None,
        "pending_action": None,
        "last_transition": None,
        "goal_complete": False,
        "iteration": 0,
        "recovery_attempts": 0,
        # False, not True: nothing has been handed shaken yet, and assuming
        # otherwise is the assumption that produces a run of false silent
        # failures.
        "handshake_done": False,
        "handshake_attempts": 0,
    }




def trace(agent: str, action: str, detail: str = "",
          **extra: Any) -> dict[str, Any]:
    """One line of the audit trail. Ends up verbatim in the report, which is
    what makes an agentic run reviewable instead of magic."""
    return {"t": round(time.time(), 3), "agent": agent, "action": action,
            "detail": detail, **extra}
