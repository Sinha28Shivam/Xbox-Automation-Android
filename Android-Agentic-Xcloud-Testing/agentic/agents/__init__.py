"""
agents - the seven specialists, in the order they run.

    1. DeviceAgent     is the phone connected and can we send it signals?
    2. ScenarioAgent   understand and VERIFY the scenario (may refuse it)
    3. PlannerAgent    compose steps from the REAL capability list
    4. ExecutorAgent   perform the testing, one step per graph tick, and observe
    5. EvaluatorAgent  verdict against the acceptance criteria
    6. RootCauseAgent  which LAYER broke, and how to disprove that
    7. ReporterAgent   JSON / Markdown / HTML

Two of these were not in the original list and are worth their keep:

* EvaluatorAgent, because judging a STEP ("did the tile move?") and judging a
  SCENARIO ("can a user reach their library?") are different questions. Merging
  them lets a run with all-green steps claim a pass for something nothing checked.

* Observation, which lives in tools/vision.py rather than as an eighth agent. It
  is called by the executor after every step, so a separate agent would only add
  a graph hop; the reason it exists at all is that a firmware OK cannot see.
"""

from .base import SYSTEM_CONTEXT, Agent
from .device import DeviceAgent
from .evaluator import EvaluatorAgent
from .executor import ExecutorAgent
from .planner import PlannerAgent
from .rca import RootCauseAgent
from .reporter import ReporterAgent
from .scenario import ScenarioAgent

__all__ = [
    "Agent", "SYSTEM_CONTEXT",
    "DeviceAgent", "ScenarioAgent", "PlannerAgent", "ExecutorAgent",
    "EvaluatorAgent", "RootCauseAgent", "ReporterAgent",
]
