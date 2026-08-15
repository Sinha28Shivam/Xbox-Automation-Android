"""
Android-Agentic-Xcloud-Testing - a multi-agent tester for xCloud on Android.

Layered so each layer can be read on its own:

    settings.py   config, with a default for every key
    schemas.py    the pydantic contracts between agents
    llm.py        runtime provider resolution + structured calls
    state.py      the LangGraph state and the live RunContext
    tools/        the only ways to touch the world (pad, adb, vision)
    agents/       seven specialists, each with a no-LLM fallback
    graph.py      the state machine that wires them together
    cli.py        the command line

Programmatic use:

    from agentic import Settings, run_test

    settings = Settings()
    settings.override("hardware.dry_run", True)
    state = run_test("open xCloud and check the controller is detected", settings)
    print(state["verdict"], state["report"].executive_summary)

TWO THINGS TO KNOW BEFORE READING THE CODE
------------------------------------------
1. xCloud is a PWA, not an installed app. There is no package to launch and no
   activity to assert on; it is a page in a browser, opened by a VIEW intent for
   a URL, and the foreground app during a test is a BROWSER by design.

2. A firmware "OK" means an HID report was queued - not that the app reacted.
   Separating those two claims is the reason this layer exists, and why a
   verdict can be `inconclusive` rather than being forced into pass or fail.
"""

from .graph import build_context, build_graph, run_test
from .schemas import (Capabilities, EnvironmentReport, Evaluation,
                      RootCauseAnalysis, ScenarioSpec, TestPlan, TestReport,
                      Verdict)
from .settings import Settings
from .state import GraphState, RunContext, new_state

__version__ = "1.0.0"

__all__ = [
    "Settings", "run_test", "build_graph", "build_context",
    "GraphState", "RunContext", "new_state",
    "Verdict", "ScenarioSpec", "TestPlan", "TestReport", "Evaluation",
    "RootCauseAnalysis", "EnvironmentReport", "Capabilities",
    "__version__",
]
