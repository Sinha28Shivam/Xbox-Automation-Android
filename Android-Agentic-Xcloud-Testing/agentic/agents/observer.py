"""
observer.py - take a look, and turn it into a GameState.

Two graph nodes live here because they are two halves of one idea:

    ObserverAgent    look at the world  -> Observation (raw sensor record)
    StateBuilder     interpret it       -> GameState   (one screen_type)

The observer deliberately does NOT judge. It takes a screenshot, diffs it,
reads it, and hands the reading on. Every judgement in this system is made
somewhere that cites its evidence, and mixing "what is on screen" with "is that
good" is how the old design ended up asserting `target_visible=True` because the
word "Minecraft" appeared in a sentence that said the tile was absent.

WHY THE OBSERVE NODE IS SEPARATE FROM THE EXECUTE NODE
------------------------------------------------------
In the closed loop the cycle begins with a look, before any action exists:

    observe -> build_state -> goal_check -> decide -> ... -> execute -> observe

The first observation of a run has no action to attribute it to, and the
observation after a recovery belongs to no action either. A node that only ran
as part of "execute" could not produce those, which is why the plan-mode
executor had to start with a hardcoded OBSERVE step in its step list.
"""

from __future__ import annotations

from ..logbook import log
from ..schemas import GameState, Goal, Observation
from ..state import GraphState
from .base import Agent


class ObserverAgent(Agent):
    """Takes ONE look and builds a GameState from it."""

    name = "observer"

    def run(self, state: GraphState) -> GraphState:
        goal: Goal | None = state.get("goal")
        previous: GameState | None = state.get("game_state")
        iteration = int(state.get("iteration", 0))

        label = f"iter{iteration:02d}_observe"
        question = self._question(goal, previous)

        observation = self.ctx.vision.observe(
            run_id=state["run_id"], label=label, question=question,
            previous_frame=self.ctx.last_frame_path)

        if observation.screenshot_path:
            self.ctx.last_frame_path = observation.screenshot_path
            self.ctx.artifacts.append(observation.screenshot_path)

        game_state = self.ctx.state_builder.build(
            observation, goal=goal, previous=previous)

        log.see(f"state: {game_state.summary()}")
        for note in game_state.evidence[:3]:
            log.debug(f"because: {note}", indent=2)

        return {
            # The state we are replacing becomes the "before" for whatever
            # action comes next, so the verifier can compare them.
            "previous_game_state": previous,
            "game_state": game_state,
            "baseline": observation if previous is None else state.get(
                "baseline"),
            "agent_trace": [self.trace(
                "observe",
                f"{game_state.summary()} sensors="
                f"{','.join(observation.sensors_used) or 'NONE'}")],
        }

    def _question(self, goal: Goal | None,
                  previous: GameState | None) -> str:
        """What to ask a vision model, IF one ends up being called.

        Note the conditional: this question is handed to `vision.observe`, which
        only reaches an LLM when `vision.llm_screen_reading` is on. The state
        builder's fast tier usually answers first, so on a confident navigation
        step this string costs nothing.
        """
        target = f" The run is trying to reach {goal.target!r}." if (
            goal and goal.target) else ""
        was = (f" The previous screen was classified as "
               f"{previous.screen_type.value}." if previous else "")
        return (f"Describe what is on this Android screen right now.{target}"
                f"{was} Report only what is VISIBLE - name the screen, any "
                f"loading indicator, any dialog or overlay, and which element "
                f"appears to be highlighted or selected. Do not infer that "
                f"anything succeeded.")


def derive_goal(spec: object, settings: object) -> Goal:
    """Build a `Goal` from a ScenarioSpec, with sane universal defaults.

    Kept as a module function rather than an agent: it is pure, it has no LLM
    call, and both the graph's setup and the tests want it without constructing
    a RunContext.

    The defaults matter. A scenario that says nothing about states still gets a
    usable goal - reaching a game main menu - because the alternative is a run
    that cannot tell success from failure and therefore cannot end. Where the
    scenario DOES declare a `judge_policy`, that wins; see `suites.py`/`scenario`
    for the parsing.
    """
    from ..schemas import ScenarioSpec, ScreenType  # local: avoid a cycle

    if not isinstance(spec, ScenarioSpec):
        return Goal()

    text = f"{spec.title} {spec.intent}".lower()

    # A launch/menu goal is the common case for this rig, so it is the default.
    success = [ScreenType.GAME_MAIN_MENU]
    intermediate = [ScreenType.GAME_DETAIL, ScreenType.FULLSCREEN_TRANSITION,
                    ScreenType.GAME_LOADING, ScreenType.GAME_CONNECTING,
                    ScreenType.LIVE_GAME_STREAM, ScreenType.GAME_SPLASH,
                    ScreenType.PRESS_ANY_BUTTON]

    if "main menu" not in text and "launch" not in text and "play" not in text:
        # A navigation-only scenario: reaching the focused tile IS the goal.
        if "focus" in text or "navigate" in text or "select" in text:
            success = [ScreenType.GAME_FOCUSED, ScreenType.GAME_DETAIL]

    return Goal(
        description=spec.intent[:300] or spec.title,
        target=None,          # filled by the scenario parser when known
        success_states=success,
        failure_states=[ScreenType.STREAM_ERROR, ScreenType.SESSION_EXPIRED],
        intermediate_states=intermediate,
        allowed_transitions={
            # THE fix for the failure that motivated this work. Pressing A on a
            # focused tile may legitimately produce any of these, and a rig that
            # admits only `game_detail` will report a successful direct launch
            # as a failure.
            ScreenType.GAME_FOCUSED.value: [
                ScreenType.GAME_DETAIL,
                ScreenType.FULLSCREEN_TRANSITION,
                ScreenType.GAME_LOADING,
                ScreenType.GAME_CONNECTING,
                ScreenType.LIVE_GAME_STREAM,
                ScreenType.GAME_SPLASH,
            ],
            ScreenType.GAME_DETAIL.value: [
                ScreenType.FULLSCREEN_TRANSITION,
                ScreenType.GAME_LOADING,
                ScreenType.GAME_CONNECTING,
                ScreenType.LIVE_GAME_STREAM,
            ],
            ScreenType.FULLSCREEN_TRANSITION.value: [
                ScreenType.GAME_LOADING,
                ScreenType.GAME_CONNECTING,
                ScreenType.LIVE_GAME_STREAM,
                ScreenType.GAME_SPLASH,
            ],
            ScreenType.GAME_LOADING.value: [
                ScreenType.GAME_LOADING,
                ScreenType.GAME_CONNECTING,
                ScreenType.LIVE_GAME_STREAM,
                ScreenType.GAME_SPLASH,
                ScreenType.PRESS_ANY_BUTTON,
                ScreenType.GAME_MAIN_MENU,
            ],
            ScreenType.LIVE_GAME_STREAM.value: [
                ScreenType.GAME_SPLASH,
                ScreenType.PRESS_ANY_BUTTON,
                ScreenType.GAME_MAIN_MENU,
                ScreenType.IN_GAME,
            ],
            ScreenType.GAME_SPLASH.value: [
                ScreenType.PRESS_ANY_BUTTON,
                ScreenType.GAME_MAIN_MENU,
                ScreenType.GAME_SPLASH,
            ],
            ScreenType.PRESS_ANY_BUTTON.value: [
                ScreenType.GAME_MAIN_MENU,
                ScreenType.IN_GAME,
            ],
            ScreenType.XCLOUD_HOME.value: [
                ScreenType.XCLOUD_HOME,
                ScreenType.GAME_FOCUSED,
                ScreenType.XCLOUD_LIBRARY,
                ScreenType.GAME_DETAIL,
            ],
        },
        max_iterations=int(getattr(settings, "get", lambda *a: 40)(
            "execution.max_iterations", 40) or 40),
    )
