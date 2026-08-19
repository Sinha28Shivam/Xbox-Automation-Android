"""
agents - the specialists, in the order they run.

SHARED BY BOTH MODES
    1. DeviceAgent     is the phone connected and can we send it signals?
    2. ScenarioAgent   understand and VERIFY the scenario (may refuse it)
    5. EvaluatorAgent  verdict against the acceptance criteria
    6. RootCauseAgent  which LAYER broke, and how to disprove that
    7. ReporterAgent   JSON / Markdown / HTML

CLOSED-LOOP MODE (execution.mode: closed_loop) - the default
    LauncherAgent      open the xCloud page, and invalidate the handshake
    HandshakeAgent     make the pad VISIBLE to the browser, and prove it
    ObserverAgent      look, and build a structured GameState

    DecisionAgent      choose exactly ONE action from that state
    ActorAgent         validate it, send it, look twice
    VerifierAgent      classify before+action+after as
                       SUCCESS / INTERMEDIATE / FAILURE / UNKNOWN
    RecoveryAgent      the cheap, LLM-free fix, tried before RCA

LEGACY PLAN MODE (execution.mode: plan | adaptive)
    3. PlannerAgent    compose a step list from the REAL capability list
    4. ExecutorAgent   walk that list, one step per graph tick

WHY BOTH STILL EXIST
--------------------
The closed loop is the correct architecture: the observed state decides the next
action, so the number of presses is an output of watching the screen rather than
a guess made before the first screenshot. The plan path is kept for one release
so a regression can be isolated by changing one config line instead of bisecting
git, and because `ExecutorAgent` still owns the two-look observation cycle that
`ActorAgent` composes with rather than duplicates.
"""

from .actor import ActorAgent
from .base import SYSTEM_CONTEXT, Agent
from .decision import DecisionAgent
from .device import DeviceAgent
from .evaluator import EvaluatorAgent
from .executor import ExecutorAgent
from .handshake import HandshakeAgent
from .launcher import LauncherAgent
from .observer import ObserverAgent, derive_goal

from .planner import PlannerAgent
from .rca import RootCauseAgent
from .recovery import RecoveryAgent
from .reporter import ReporterAgent
from .scenario import ScenarioAgent
from .verifier import VerifierAgent

__all__ = [
    "Agent", "SYSTEM_CONTEXT",
    # shared
    "DeviceAgent", "ScenarioAgent", "EvaluatorAgent", "RootCauseAgent",
    "ReporterAgent",
    # closed loop
    "LauncherAgent", "HandshakeAgent", "ObserverAgent", "DecisionAgent",
    "ActorAgent", "VerifierAgent", "RecoveryAgent", "derive_goal",

    # legacy plan mode
    "PlannerAgent", "ExecutorAgent",
]
