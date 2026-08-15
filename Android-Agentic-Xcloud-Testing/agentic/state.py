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
from .schemas import (Capabilities, EnvironmentReport, Evaluation, Observation,
                      RootCauseAnalysis, ScenarioSpec, StepResult, TestPlan,
                      TestReport, Verdict)
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
    run_id: str = ""
    started_at: float = field(default_factory=time.time)
    artifacts: list[str] = field(default_factory=list)
    # Kept so ObserverAgent can diff the current frame against the last one.
    last_frame_path: str | None = None

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

    # -- control flow -----------------------------------------------------
    cursor: int                  # index of the next step to execute
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
    }


def trace(agent: str, action: str, detail: str = "",
          **extra: Any) -> dict[str, Any]:
    """One line of the audit trail. Ends up verbatim in the report, which is
    what makes an agentic run reviewable instead of magic."""
    return {"t": round(time.time(), 3), "agent": agent, "action": action,
            "detail": detail, **extra}
