"""
graph.py - the LangGraph state machine that wires the agents together.

TWO SHAPES, SELECTED BY `execution.mode`
========================================

CLOSED LOOP (mode: closed_loop) - the default and the correct architecture
-------------------------------------------------------------------------

    device -> scenario -> strategy -> launch -> handshake -> observe -> goal_check
                                                    ^  |        ^            |
                                          (until confirmed)     |            v
                                                                |    recover <- verify <- act <- decide
                                                                |       |          |
                                                                +-------+          +--> goal_check
                                                                        v               (success/intermediate)
                                                                       rca
                                                                        |
                                                                        +--> evaluate -> report

    BOOTSTRAP: `launch` then `handshake`, once per page load.

    The handshake is not a convenience - it is a browser protocol step. The W3C
    Gamepad API hides a gamepad from a page until that pad sends a button event,
    so a freshly loaded xCloud page CANNOT SEE the controller no matter how
    correctly the Leonardo is wired. Guide x2 makes it visible; B puts the UI
    back. Until that has happened every input is discarded by the PAGE, which
    looks exactly like a wiring fault and wastes the whole run diagnosing one.

    `launch` sets `handshake_done = False`, so this repeats after EVERY page
    load - including a mid-run reload - rather than only at startup.

    The cycle is then:  observe -> decide ONE -> execute -> observe -> verify.


    The essential property is that `decide` runs AFTER `observe`, every pass.
    The number of presses needed to cross a menu is therefore an OUTPUT of
    watching where the highlight went, not a number guessed before the first
    screenshot existed. Everything else follows from that.

    `verify` classifies the transition as SUCCESS / INTERMEDIATE / FAILURE /
    UNKNOWN, and only FAILURE may leave the loop. That single change is what
    stops a correct game launch - which legitimately lands in a fullscreen
    handoff and then a loading screen - from being recorded as a failure and
    sent to root-cause analysis.

    `recover` sits between `verify` and `rca` and handles the common,
    uninteresting failures (still loading, an overlay stealing input, an
    unreadable frame) with no LLM call at all. RCA is reserved for attributing a
    real fault to a LAYER, which is what it is good at.

LEGACY PLAN MODE (mode: plan | adaptive)
----------------------------------------

    device -> scenario -> plan -> execute -> (loop) -> evaluate -> report
                 |          |        |                    |
                 +----------+--------+--> rca -> replan --+

    Kept for one release so a regression can be isolated with a single config
    change rather than a bisect.

INVARIANTS THAT HOLD IN BOTH SHAPES
-----------------------------------
* ONE step per node tick, then back to a router. Control flow stays visible in
  the graph and in the trace instead of hiding inside an agent's own loop.
* Every edge that can end the run leads to `report`. A run that produces no
  report is indistinguishable from a crash, so there is no path that skips it.
* `finally: pad.close()` in `run_test`. Non-negotiable: a crash mid-`stick()`
  otherwise leaves an axis deflected and the character walking into a wall.
* The router functions are deliberately dull. Control flow is the one place that
  must NOT be a judgement call - the LLM decides what to do, the graph decides
  what happens next, and keeping those separate is what makes a run reproducible
  enough to debug.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .agents import (ActorAgent, DecisionAgent, DeviceAgent, EvaluatorAgent,
                     ExecutorAgent, HandshakeAgent, LauncherAgent,
                     ObserverAgent, PlannerAgent, ReporterAgent, RecoveryAgent,
                     RootCauseAgent, ScenarioAgent, VerifierAgent, derive_goal)

from .llm import LLMFactory
from .logbook import log
from .schemas import TransitionClass, Verdict
from .settings import Settings
from .state import GraphState, RunContext, new_state, trace
from .timing import Timing
from .tools import AndroidTool, PadTool, VisionTool

# Modes that use the closed loop. Anything else falls back to the plan walker.
CLOSED_LOOP_MODES = ("closed_loop", "closed-loop", "closedloop", "loop")


# ==========================================================================
# Context construction
# ==========================================================================
def build_context(settings: Settings, run_id: str) -> RunContext:
    """Wire up the live resources. Order matters: vision needs android."""
    llm = LLMFactory(settings)
    android = AndroidTool(settings)
    vision = VisionTool(settings, llm, android)
    return RunContext(
        settings=settings,
        llm=llm,
        pad=PadTool(settings),
        android=android,
        vision=vision,
        # One Timing for the whole run, so `total_waited` is a single number the
        # report can quote instead of a sum scattered across agents.
        timing=Timing(settings),
        run_id=run_id,
    )


def is_closed_loop(settings: Settings) -> bool:
    mode = str(settings.get("execution.mode", "closed_loop")).lower().strip()
    return mode in CLOSED_LOOP_MODES


# ==========================================================================
# Routers - dull on purpose
# ==========================================================================
def _halted(state: GraphState) -> bool:
    return bool(state.get("halt_reason"))


def route_after_device(state: GraphState) -> str:
    """A dead link means no test is possible. Report the blockage and stop."""
    return "report" if _halted(state) else "scenario"


def route_after_scenario(state: GraphState) -> str:
    """An untestable scenario is a finding, not a failure. Do not touch the pad."""
    return "report" if _halted(state) else "strategy"


def route_after_strategy(state: GraphState) -> str:
    return "report" if _halted(state) else "observe"


# -- closed loop: bootstrap ------------------------------------------------
def route_after_launch(state: GraphState) -> str:
    """A launch is always followed by the handshake, never by an observation.

    Deliberately unconditional. A fresh page cannot see the gamepad until the
    pad sends a button report, so observing first would photograph a screen that
    is about to be interacted with by a controller it does not yet know exists.
    """
    return "report" if _halted(state) else "handshake"


def route_after_handshake(state: GraphState) -> str:
    """Loop on the handshake until it is confirmed, then start observing.

    The retry edge points back at `handshake` rather than at `launch`: the page
    is fine, it simply has not acknowledged the pad yet, and reloading would
    throw away a perfectly good page load. The handshake agent owns its own
    attempt budget and sets `halt_reason` when it runs out, so this router can
    stay dull.
    """
    if _halted(state):
        return "report"
    return "observe" if state.get("handshake_done") else "handshake"


# -- closed loop -----------------------------------------------------------
def route_after_observe(state: GraphState) -> str:
    return "report" if _halted(state) else "goal_check"



def route_after_goal_check(state: GraphState) -> str:
    """The only place the loop is allowed to end normally."""
    if _halted(state):
        return "report"
    if state.get("goal_complete"):
        return "evaluate"
    return "decide"


def route_after_decide(state: GraphState) -> str:
    if _halted(state):
        return "report"
    # A decision that produced nothing is a harness fault, not a device
    # finding - evaluate what we have rather than dispatching a null action.
    return "act" if state.get("pending_action") is not None else "evaluate"


def route_after_act(state: GraphState) -> str:
    if _halted(state):
        return "report"
    if state.get("goal_complete"):
        # The decision agent answered DONE, so there is no transition to verify.
        return "evaluate"
    return "verify"


def route_after_verify(state: GraphState) -> str:
    """SUCCESS, INTERMEDIATE and UNKNOWN all continue the loop.

    This is the routing change that matters. Under the old two-valued judgement
    anything that was not the predicted screen went to RCA; now only a genuine
    FAILURE leaves the loop, and a valid intermediate state - a loading screen,
    a fullscreen handoff - simply goes round again and waits.
    """
    if _halted(state):
        return "report"
    transition = state.get("last_transition")
    if transition is None:
        return "goal_check"
    if transition.goal_complete:
        return "evaluate"
    if transition.classification is TransitionClass.FAILURE:
        return "recover"
    return "goal_check"


def route_after_recover(state: GraphState) -> str:
    """Recovery either sends us back to look again, or gives up to RCA."""
    if _halted(state):
        return "report"
    return "rca" if state.get("needs_rca") else "observe"


# -- legacy plan mode ------------------------------------------------------
def route_after_plan(state: GraphState) -> str:
    return "report" if _halted(state) else "execute"


def route_after_execute(state: GraphState) -> str:
    """The heart of legacy `adaptive` mode."""
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
    closed_loop = is_closed_loop(ctx.settings)

    device = DeviceAgent(ctx)
    scenario = ScenarioAgent(ctx)
    planner = PlannerAgent(ctx)
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
            # The acting agents announce their own headers (they know the step
            # or iteration number), so they are not double-announced here.
            quiet = agent.name in ("executor", "actor")
            if not quiet:
                log.node(f"--> {agent.name}")
            started = time.time()
            try:
                result = agent.run(state)
                if not quiet:
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

    def strategy_node(state: GraphState) -> GraphState:
        """Derive the GOAL, and in plan mode also build the step list.

        In the closed loop the planner is demoted to exactly this: work out what
        state the run is trying to reach. It no longer decides the route, because
        the route is decided one observation at a time.
        """
        spec = state.get("scenario")
        goal = derive_goal(spec, ctx.settings)

        # Refresh the validator now that capabilities are known. Constructed
        # empty in RunContext so that a wiring bug which skipped discovery
        # rejects every control loudly instead of silently sending buttons the
        # rig may not have.
        caps = state.get("capabilities")
        if caps is not None:
            from .control import ActionValidator
            prohibited = _prohibited_inputs(state)
            ctx.validator = ActionValidator(ctx.settings, caps, prohibited)
            ctx.state_builder.s = ctx.settings
            log.kv("strategy", controls=ctx.validator.describe_policy())

        # The target the run is navigating toward, taken from the scenario text
        # rather than hardcoded anywhere.
        goal.target = _target_from_scenario(state)

        log.kv("strategy",
               goal=goal.description[:70] or "unspecified",
               target=goal.target or "none",
               success="/".join(s.value for s in goal.success_states))

        out: GraphState = {
            "goal": goal,
            "agent_trace": [trace(
                "strategy", "goal derived",
                f"target={goal.target!r} success="
                f"{','.join(s.value for s in goal.success_states)}")],
        }
        return out

    def goal_check_node(state: GraphState) -> GraphState:
        """Has the goal been reached, or has the loop run out of room?

        A pure predicate with a bounds check. Deliberately NOT an LLM call: a
        model that could declare the run complete would be able to end it by
        being agreeable, which is exactly what the evaluator's verdict ceiling
        exists to prevent.
        """
        goal = state.get("goal")
        game_state = state.get("game_state")
        iteration = int(state.get("iteration", 0))
        limit = int(ctx.settings.get("execution.max_iterations", 40))

        if goal is not None and game_state is not None and goal.is_success(
                game_state):
            log.ok(f"GOAL REACHED: {game_state.screen_type.value} "
                   f"(confidence {game_state.confidence:.0%})")
            return {
                "goal_complete": True,
                "agent_trace": [trace(
                    "goal_check", "goal reached",
                    f"{game_state.screen_type.value} at "
                    f"{game_state.confidence:.0%} confidence")],
            }

        if iteration >= limit:
            return {
                "halt_reason": (
                    f"the closed loop used all {limit} iterations "
                    f"(execution.max_iterations) without reaching the goal"),
                "agent_trace": [trace("goal_check", "iteration limit",
                                      f"{iteration}/{limit}")],
            }

        # -- the BLIND LOOP guard ---------------------------------------
        #
        # A run with no working sensors classifies every screen as UNKNOWN at 0%
        # confidence, so the decision agent correctly answers OBSERVE - and then
        # observes again, and again, for the full iteration budget. Every
        # individual decision is right; the aggregate is a livelock that spends
        # half an hour proving nothing.
        #
        # `max_iterations` does eventually stop it, but "eventually" is ~30
        # minutes at ~45s per pass, and the report at the end says exactly what
        # could have been said after the second pass: this rig cannot see.
        #
        # So: three consecutive states with NO sensor data at all is a rig
        # finding, not something to keep retrying. Note the bar - it requires
        # `sensors_used` to be empty, not merely low confidence. An unfamiliar
        # screen that OCR can read but not classify is a genuine perception
        # problem worth more looking; a screen nobody photographed is not.
        blind_limit = int(ctx.settings.get(
            "execution.closed_loop.max_blind_iterations", 3))
        blind = 0
        for result in reversed(list(state.get("step_results", []))):
            after = result.game_state_after
            obs = after.observation if after is not None else None
            if obs is not None and obs.sensors_used:
                break
            blind += 1
        if blind >= blind_limit and iteration >= blind_limit:
            return {
                "halt_reason": (
                    f"{blind} consecutive observations produced NO sensor data "
                    f"at all, so the loop is blind and would spend the "
                    f"remaining {limit - iteration} iterations re-observing "
                    f"nothing. This is a rig problem, not an xCloud finding: "
                    f"without adb there are no screenshots, and without "
                    f"screenshots a firmware OK cannot prove the app reacted. "
                    f"Connect adb over Wi-Fi (`adb tcpip 5555` then "
                    f"`adb connect <phone-ip>:5555`) and re-run."),
                "agent_trace": [trace(
                    "goal_check", "blind loop",
                    f"{blind} sensorless observations in a row at iteration "
                    f"{iteration}/{limit}")],
            }


        if ctx.out_of_time():
            return {
                "halt_reason": (
                    f"run exceeded safety.max_run_seconds "
                    f"({ctx.settings.get('safety.max_run_seconds')}s)"),
                "agent_trace": [trace("goal_check", "time budget exhausted")],
            }

        return {
            "goal_complete": False,
            "agent_trace": [trace(
                "goal_check", "continuing",
                f"iteration {iteration}/{limit}, state="
                + (game_state.screen_type.value if game_state else "unknown"))],
        }

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
            # A replan also forgives the recovery budget: this is a fresh
            # attempt at the goal, not a continuation of the failed one.
            "recovery_attempts": 0,
            "adaptations": [
                f"replan {attempt}/{max_replans}: "
                + (rca_result.retry_strategy if rca_result
                   and rca_result.retry_strategy else "retrying")],
            "agent_trace": [trace("graph", "replan",
                                  f"attempt {attempt}/{max_replans}")],
        }

    graph = StateGraph(GraphState)

    # -- shared nodes --------------------------------------------------
    graph.add_node("device", node(device))
    graph.add_node("scenario", node(scenario))
    graph.add_node("strategy", strategy_node)
    graph.add_node("evaluate", node(evaluator))
    graph.add_node("rca", node(rca))
    graph.add_node("report", node(reporter))

    graph.set_entry_point("device")
    graph.add_conditional_edges("device", route_after_device,
                                {"scenario": "scenario", "report": "report"})
    graph.add_conditional_edges("scenario", route_after_scenario,
                                {"strategy": "strategy", "report": "report"})
    graph.add_edge("evaluate", "report")
    graph.add_edge("report", END)

    if closed_loop:
        launcher = LauncherAgent(ctx)
        handshake = HandshakeAgent(ctx)
        observer = ObserverAgent(ctx)
        decider = DecisionAgent(ctx)
        actor = ActorAgent(ctx)
        verifier = VerifierAgent(ctx)
        recovery = RecoveryAgent(ctx)

        # The BOOTSTRAP pair. These run once per page load and exist because a
        # fresh browser page cannot see a gamepad until the pad sends a button
        # report - so a loop that started observing immediately would be driving
        # a controller the page does not know exists.
        graph.add_node("launch", node(launcher))
        graph.add_node("handshake", node(handshake))

        graph.add_node("observe", node(observer))
        graph.add_node("goal_check", goal_check_node)
        graph.add_node("decide", node(decider))
        graph.add_node("act", node(actor))
        graph.add_node("verify", node(verifier))
        graph.add_node("recover", node(recovery))

        # strategy -> LAUNCH (not observe): open the page before looking at it.
        graph.add_conditional_edges("strategy", route_after_strategy,
                                    {"observe": "launch", "report": "report"})
        graph.add_conditional_edges("launch", route_after_launch,
                                    {"handshake": "handshake",
                                     "report": "report"})
        # The handshake loops on ITSELF until confirmed. Its own attempt budget
        # decides when to give up, so this edge cannot spin forever.
        graph.add_conditional_edges("handshake", route_after_handshake,
                                    {"observe": "observe",
                                     "handshake": "handshake",
                                     "report": "report"})
        graph.add_conditional_edges("observe", route_after_observe,
                                    {"goal_check": "goal_check",
                                     "report": "report"})

        graph.add_conditional_edges("goal_check", route_after_goal_check,
                                    {"decide": "decide",
                                     "evaluate": "evaluate",
                                     "report": "report"})
        graph.add_conditional_edges("decide", route_after_decide,
                                    {"act": "act", "evaluate": "evaluate",
                                     "report": "report"})
        graph.add_conditional_edges("act", route_after_act,
                                    {"verify": "verify",
                                     "evaluate": "evaluate",
                                     "report": "report"})
        graph.add_conditional_edges("verify", route_after_verify,
                                    {"goal_check": "goal_check",
                                     "recover": "recover",
                                     "evaluate": "evaluate",
                                     "report": "report"})
        graph.add_conditional_edges("recover", route_after_recover,
                                    {"observe": "observe", "rca": "rca",
                                     "report": "report"})
        # RCA in the closed loop does not replan a step list - there is none.
        # It diagnoses, and the run ends with that diagnosis in the report.
        graph.add_edge("rca", "evaluate")
    else:
        executor = ExecutorAgent(ctx)
        graph.add_node("plan", node(planner))
        graph.add_node("execute", node(executor))
        graph.add_node("replan", replan_node)

        graph.add_conditional_edges("strategy", route_after_strategy,
                                    {"observe": "plan", "report": "report"})
        graph.add_conditional_edges("plan", route_after_plan,
                                    {"execute": "execute", "report": "report"})
        graph.add_conditional_edges("execute", route_after_execute,
                                    {"execute": "execute", "rca": "rca",
                                     "evaluate": "evaluate",
                                     "report": "report"})
        graph.add_conditional_edges("rca", route_after_rca,
                                    {"replan": "replan",
                                     "evaluate": "evaluate"})
        # The replan path deliberately re-enters the PLANNER, not the executor:
        # the point of a replan is a different plan, not the same steps again.
        graph.add_edge("replan", "plan")

    return graph.compile()


# ==========================================================================
# Scenario helpers
# ==========================================================================
def _target_from_scenario(state: GraphState) -> str | None:
    """Work out what the run is navigating TOWARD, from the scenario text.

    Reads the scenario, never a hardcoded game name - the previous design had
    `if "minecraft" in combined` inside the perception layer, which meant adding
    a second game required editing the code that reads pixels.
    """
    spec = state.get("scenario")
    if spec is None:
        return None

    raw = state.get("raw_scenario", "") or ""

    # 1. an explicit `target:` block in the YAML wins
    #
    # ...but `target:` is also the key a LAUNCH_PWA step uses for its URL, and
    # a scenario listing steps will hit that one FIRST. A run whose navigation
    # target is "https://www.xbox.com/play" then looks for that string on screen,
    # never finds it, and never focuses the game. So URLs and other non-titles
    # are skipped rather than taken as the target.
    for line in raw.splitlines():
        stripped = line.strip()
        for key in ("target:", "game:", "target_game:"):
            if stripped.lower().startswith(key):
                value = stripped[len(key):].strip().strip("\"'")
                if not value or value.startswith(("{", "[")):
                    continue
                low = value.lower()
                if low.startswith(("http://", "https://", "www.")):
                    continue                      # a URL, not a game
                if low in ("reach_main_menu", "null", "none", "~"):
                    continue                      # a goal TYPE, not a title
                return value


    # 2. otherwise look for a quoted proper noun in the title/intent
    import re
    for text in (spec.title, spec.intent):
        match = re.search(r"['\"]([A-Z][\w:'\- ]{2,40})['\"]", text or "")
        if match:
            return match.group(1).strip()

    # 3. finally, a capitalised multi-word phrase in the title
    match = re.search(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})\b",
                      spec.title or "")
    if match:
        candidate = match.group(1).strip()
        # Filter the obvious non-titles a scenario title tends to contain.
        if candidate.lower() not in ("launch", "the xcloud", "xbox cloud"):
            return candidate
    return None


def _prohibited_inputs(state: GraphState) -> list[str]:
    """Read `controller_policy.prohibited_inputs` out of the raw scenario.

    Parsed from the text rather than from ScenarioSpec because this is a hard
    constraint that must survive whatever an LLM made of the prose. A scenario
    that says "gamepad only" and is then quietly satisfied by an ADB keyevent
    has proved nothing about the controller path it claims to test, so the ban
    is taken literally from what the author wrote.
    """
    raw = state.get("raw_scenario", "") or ""
    if "prohibited_inputs" not in raw:
        return []
    try:
        import yaml
        parsed = yaml.safe_load(raw)
    except Exception:                                # noqa: BLE001
        return []
    if not isinstance(parsed, dict):
        return []
    policy = parsed.get("controller_policy")
    if not isinstance(policy, dict):
        return []
    banned = policy.get("prohibited_inputs")
    return [str(b) for b in banned] if isinstance(banned, list) else []


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
    closed_loop = is_closed_loop(settings)
    log.rule(f"RUN {state['run_id']}")
    log.kv("run",
           mode=settings.get("execution.mode", "closed_loop"),
           control=("closed loop (observe -> decide one -> execute -> verify)"
                    if closed_loop else "plan walker (legacy)"),
           dry_run=settings.get("hardware.dry_run", False),
           llm=settings.get("llm.active", "?"),
           glance=settings.get("execution.settle.glance_enabled", True),
           settle_scale=settings.get("execution.settle.scale", 1.0))
    log.kv("run", scenario=source,
           text=(scenario_text or "")[:90].replace("\n", " "))

    app = build_graph(ctx)

    # A step count generous enough for the whole loop plus the replan cycles, so
    # LangGraph's own recursion guard never fires before ours does. The closed
    # loop spends ~6 nodes per iteration (observe, goal_check, decide, act,
    # verify, and sometimes recover), so the per-iteration cost is higher than
    # plan mode's one-node tick and the limit must account for it.
    if closed_loop:
        iterations = int(settings.get("execution.max_iterations", 40))
        recursion_limit = iterations * 7 + 20
    else:
        max_steps = int(settings.get("execution.max_steps", 60))
        replans = int(settings.get("retry.max_replans", 2))
        recursion_limit = (max_steps + 8) * (replans + 1) + 12

    if progress:
        progress(f"run {state['run_id']} starting "
                 f"(mode={settings.get('execution.mode', 'closed_loop')}, "
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
    # the previous 517-second run could not: how much of that was waiting, how
    # much was perception, and whether the cheap tier earned its keep.
    log.rule(f"RUN {state['run_id']} -> {label.upper()}")
    log.kv("run", verdict=label, elapsed=f"{ctx.elapsed():.1f}s",
           llm_calls=ctx.llm.calls, steps=len(final.get("step_results", [])),
           replans=final.get("replans", 0))
    if closed_loop:
        builder = ctx.state_builder
        log.kv("run",
               iterations=final.get("iteration", 0),
               transitions=len(final.get("transitions", [])),
               states_fast=getattr(builder, "fast_resolved", 0),
               states_escalated=getattr(builder, "escalated", 0))
    log.kv("run", waits=ctx.timing.summary())
    log.summary()
    log.close()

    if progress:
        progress(f"run {state['run_id']} finished: "
                 f"{label} "
                 f"in {ctx.elapsed():.1f}s "
                 f"({ctx.llm.calls} LLM calls)")

    return final
