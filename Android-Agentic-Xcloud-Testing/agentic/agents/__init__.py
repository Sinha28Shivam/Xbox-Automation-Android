"""Agent package exports and closed-loop runtime guards."""

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

# Install deterministic guards only after all agent classes are imported. This
# avoids circular imports while ensuring every closed-loop run gets the same
# launch watchdog and Android unexpected-screen handling.
from ..runtime_guards import install_runtime_guards
install_runtime_guards()

__all__ = [
    "Agent", "SYSTEM_CONTEXT",
    "DeviceAgent", "ScenarioAgent", "EvaluatorAgent", "RootCauseAgent",
    "ReporterAgent",
    "LauncherAgent", "HandshakeAgent", "ObserverAgent", "DecisionAgent",
    "ActorAgent", "VerifierAgent", "RecoveryAgent", "derive_goal",
    "PlannerAgent", "ExecutorAgent",
]
