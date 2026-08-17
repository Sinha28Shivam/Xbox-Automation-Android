"""
graph.py - the LangGraph state machine that wires the agents together.

    device -> scenario -> plan -> execute -> (loop) -> evaluate -> report
                 |          |         |                    |
                 +----------+---------+--> rca -> replan ---+
                        (halt)                  or -> report

THE SHAPE, AND WHY
------------------
* ONE step per `execute` tick, then back to the router. That is what makes
  `adaptive` mode possible: the graph can divert to RCA the moment a step's
  expectation fails, instead of grinding through the rest of a plan that is
  already off the rails. Putting the loop inside the executor would hide that
  decision from the graph and from the trace.

* RCA sits on the FAILURE path, not the end. Diagnosing while the evidence is
  fresh - and before deciding whether to replan - is the whole value of having it.

* Every edge that can end the run leads to `report`. A run that produces no
  report is indistinguishable from a crash, so there is no path that skips it.

* `finally: pad.close()` in `run_test`. Non-negotiable: pad_link's own docs make
  the point that a crash mid-`stick()` otherwise leaves an axis deflected and the
  character walking into a wall forever.

The router functions are deliberately dull. Control flow in an agentic system is
the one place that must NOT be a judgement call - the LLM decides what to do, the
graph decides what happens next, and keeping those separate is what makes a run
reproducible enough to debug.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .agents import (DeviceAgent, EvaluatorAgent, ExecutorAgent, PlannerAgent,
                     ReporterAgent, RootCauseAgent, ScenarioAgent)
from .llm import LLMFactory
from .logbook import log
from .schemas import Verdict
from .settings import Settings
from .state import GraphState, RunContext, new_state, trace
from .timing import Timing
from .tools import AndroidTool, PadTool, VisionTool


# ==========================================================================
# Context construction
# ==========================================================================
def build_context(settings: Settings, run_id: str) -> RunContext:
    """Wire up the live resources. Order matters: vision needs android."""
    llm = LLMFactory(settings)
    android = AndroidTool(settings)
    return RunContext(
        settings=settings,
        llm=llm,
        pad=PadTool(settings),
        android=android,
        vision=VisionTool(settings, llm, android),
        # One Timing for the whole run, so `total_waited` is a single number the
        # report can quote instead of a sum scattered across agents.
        timing=Timing(settings),
        run_id=run_id,
    )



# ==========================================================================
# Routers
# ==========================================================================
def _halted(state: GraphState) -> bool:
    return bool(state.get("halt_reason"))


def route_after_device(state: GraphState) -> str:
    """A dead link means no test is possible. Report the blockage and stop."""
    return "report" if _halted(state) else "scenario"


def route_after_scenario(state: GraphState) -> str:
    """An untestable scenario is a finding, not a failure. Do not touch the pad."""
    return "report" if _halted(state) else "plan"


def route_after_plan(state: GraphState) -> str:
    return "report" if _halted(state) else "execute"


def route_after_execute(state: GraphState) -> str:
    """The heart of `adaptive` mode."""
    if _halted(state):
        return "report"

    if state.get("needs_rca"):
        # Diagnose NOW, while the screenshot and the log excerpt still describe
        # the moment of failure.
        return "rca"

    plan = state.get("plan")
    cursor = int(state.get("cursor", 0))
    if plan is not None and cursor < len(plan.steps):
        return "execute"
    return "evaluate"


def route_after_rca(state: GraphState) -> str:
    """Replan only when the diagnosis says a retry can plausibly differ.

    `retryable` is decided in CODE by the RCA agent, which already checks both
    the cause allowlist and the replan budget. So this router just obeys it - a
    wiring fault cannot talk its way into three identical runs.
    """
    rca = state.get("root_cause")
    if rca is None:
        return "evaluate"
    return "replan" if rca.retryable else "evaluate"


# ==========================================================================
# Graph
# ==========================================================================
def build_graph(ctx: RunContext) -> Any:
    """Compile the graph. Agents are instantiated once and closed over."""
    device = DeviceAgent(ctx)
    scenario = ScenarioAgent(ctx)
    planner = PlannerAgent(ctx)
    executor = ExecutorAgent(ctx)
    evaluator = EvaluatorAgent(ctx)
    rca = RootCauseAgent(ctx)
    reporter = ReporterAgent(ctx)

    max_replans = int(ctx.settings.get("retry.max_replans", 2))

    def node(agent: Any) -> Callable[[GraphState], GraphState]:
        """Wrap an agent so its own crash becomes a reportable error.

        Without this an exception in one agent kills the process and produces no
        report - the one outcome worse than a wrong verdict, because it tells you
        nothing at all.
        """
        def _invoke(state: GraphState) -> GraphState:
            # The executor announces its own step header (it knows the step
            # number), so it is not double-announced here.
            if agent.name != "executor":
                log.node(f"--> {agent.name}")
            started = time.time()
            try:
                result = agent.run(state)
                if agent.name != "executor":
                    log.node(f"<-- {agent.name} finished in "
                             f"{time.time() - started:.1f}s")
                return result
            except Exception as exc:                 # noqa: BLE001
                import traceback
                detail = f"{type(exc).__name__}: {exc}"
                # Logged as well as returned: a crash inside an agent is the one
                # event a reader most needs to see WHERE it happened, and the
                # report alone cannot show the ordering against the waits.
                log.error(f"agent '{agent.name}' CRASHED after "
                          f"{time.time() - started:.1f}s - {detail}")
                return {
                    "halt_reason": f"agent '{agent.name}' crashed - {detail}",
                    "verdict": Verdict.ERROR,
                    "errors": [f"{agent.name}: {detail}\n"
                               f"{traceback.format_exc(limit=6)}"],
                    "agent_trace": [trace(agent.name, "crash", detail)],
                }
        return _invoke


    def replan_node(state: GraphState) -> GraphState:
        """Reset the execution cursor and count the attempt.

        `needs_rca` must be cleared here or the router would send us straight
        back to RCA on the first tick of the new plan.
        """
        attempt = int(state.get("replans", 0)) + 1
        rca_result = state.get("root_cause")
        return {
            "replans": attempt,
            "cursor": 0,
            "needs_rca": False,
            "adaptations": [
                f"replan {attempt}/{max_replans}: "
                + (rca_result.retry_strategy if rca_result
                   and rca_result.retry_strategy else "retrying")],
            "agent_trace": [trace("graph", "replan",
                                  f"attempt {attempt}/{max_replans}")],
        }

    graph = StateGraph(GraphState)

    graph.add_node("device", node(device))
    graph.add_node("scenario", node(scenario))
    graph.add_node("plan", node(planner))
    graph.add_node("execute", node(executor))
    graph.add_node("evaluate", node(evaluator))
    graph.add_node("rca", node(rca))
    graph.add_node("replan", replan_node)
    graph.add_node("report", node(reporter))

    graph.set_entry_point("device")

    graph.add_conditional_edges("device", route_after_device,
                                {"scenario": "scenario", "report": "report"})
    graph.add_conditional_edges("scenario", route_after_scenario,
                                {"plan": "plan", "report": "report"})
    graph.add_conditional_edges("plan", route_after_plan,
                                {"execute": "execute", "report": "report"})
    graph.add_conditional_edges("execute", route_after_execute,
                                {"execute": "execute", "rca": "rca",
                                 "evaluate": "evaluate", "report": "report"})
    graph.add_conditional_edges("rca", route_after_rca,
                                {"replan": "replan", "evaluate": "evaluate"})
    # The replan path deliberately re-enters the PLANNER, not the executor: the
    # point of a replan is a different plan, not the same steps again.
    graph.add_edge("replan", "plan")
    graph.add_edge("evaluate", "report")
    graph.add_edge("report", END)

    return graph.compile()


# ==========================================================================
# Entry point
# ==========================================================================
def run_test(scenario_text: str, settings: Settings,
             source: str = "<cli>", run_id: str | None = None,
             progress: Callable[[str], None] | None = None) -> GraphState:
    """Run one scenario end to end and return the final state.

    Always returns - a blocked or crashed run still yields a state with a report,
    because "no output" is the least useful test result there is.
    """
    state = new_state(scenario_text, source, run_id)
    ctx = build_context(settings, state["run_id"])
    ctx.started_at = time.time()

    # Start the live transcript BEFORE anything can block. The first thing a
    # run does is open a serial port and probe adb, and both can hang for
    # seconds; if the log is configured after that, the console is silent during
    # exactly the window where a reader most needs to know what is happening.
    log.configure(
        run_id=state["run_id"],
        level=str(settings.get("logs.level", "info")),
        file_path=(settings.artifact_dir(state["run_id"]) / "run.log"
                   if settings.get("logs.file_enabled", True) else None),
        colour=settings.get("logs.colour", None),
    )
    log.rule(f"RUN {state['run_id']}")
    log.kv("run",
           mode=settings.get("execution.mode", "adaptive"),
           dry_run=settings.get("hardware.dry_run", False),
           llm=settings.get("llm.active", "?"),
           glance=settings.get("execution.settle.glance_enabled", True),
           settle_scale=settings.get("execution.settle.scale", 1.0))
    log.kv("run", scenario=source,
           text=(scenario_text or "")[:90].replace("\n", " "))

    app = build_graph(ctx)

    # A step count generous enough for max_steps executor ticks plus the replan
    # cycles, so LangGraph's own recursion guard never fires before ours does.
    max_steps = int(settings.get("execution.max_steps", 60))
    replans = int(settings.get("retry.max_replans", 2))
    recursion_limit = (max_steps + 8) * (replans + 1) + 12

    if progress:
        progress(f"run {state['run_id']} starting "
                 f"(mode={settings.get('execution.mode', 'adaptive')}, "
                 f"dry_run={settings.get('hardware.dry_run', False)})")

    try:

        final: GraphState = app.invoke(
            state, config={"recursion_limit": recursion_limit})
    except Exception as exc:                         # noqa: BLE001
        # The graph itself failed (recursion limit, checkpointer, a bug here).
        # Salvage what we have rather than losing the run entirely.
        import traceback
        final = dict(state)                          # type: ignore[assignment]
        final["verdict"] = Verdict.ERROR
        final["halt_reason"] = f"graph execution failed: {exc}"
        final.setdefault("errors", []).append(traceback.format_exc(limit=8))
        log.error(f"the GRAPH itself failed: {exc}")
        try:
            final.update(ReporterAgent(ctx).run(final))
        except Exception:                            # noqa: BLE001
            pass
    finally:
        # ALWAYS release the controls and the port, whatever happened above.
        try:
            ctx.pad.close()
            log.debug("pad link closed and every control released")
        except Exception:                            # noqa: BLE001
            pass

    verdict = final.get("verdict", Verdict.INCONCLUSIVE)
    label = getattr(verdict, "value", str(verdict))

    # Close the transcript with where the time went. This answers the question
    # the previous 338-second run could not: how much of that was waiting, and
    # was it the waiting that cost us the evidence.
    log.rule(f"RUN {state['run_id']} -> {label.upper()}")
    log.kv("run", verdict=label, elapsed=f"{ctx.elapsed():.1f}s",
           llm_calls=ctx.llm.calls, steps=len(final.get("step_results", [])),
           replans=final.get("replans", 0))
    log.kv("run", waits=ctx.timing.summary())
    log.summary()
    log.close()

    if progress:
        progress(f"run {state['run_id']} finished: "
                 f"{label} "
                 f"in {ctx.elapsed():.1f}s "
                 f"({ctx.llm.calls} LLM calls)")

    return final


