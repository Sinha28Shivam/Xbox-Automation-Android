"""
verify_loop_cycle.py - drive the closed loop's ROUTERS through a real launch.

The other two verify scripts check the pieces. This one checks the CYCLE: it
replays the exact state sequence from the run that failed, and asserts the
routers now send it somewhere sensible at every hop.

    GAME_FOCUSED --press a--> FULLSCREEN_TRANSITION --wait--> GAME_LOADING
                 --wait--> GAME_SPLASH --press a--> GAME_MAIN_MENU

Under the old design, hop 2 produced `expectation_met=False` (the detail page it
predicted never appeared), which routed to RCA and then re-pressed A. Every hop
below must now stay inside the loop until the goal is genuinely reached.

No hardware, no LLM, no network.

Run:  python verify_loop_cycle.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentic.agents import derive_goal                           # noqa: E402
from agentic.graph import (route_after_goal_check,               # noqa: E402
                           route_after_verify)
from agentic.schemas import (Action, ActionType, GameState,       # noqa: E402
                             ScenarioSpec, ScreenType, Transition,
                             TransitionClass)
from agentic.settings import Settings                            # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str) -> None:
    results.append((ok, name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


settings = Settings(use_dotenv=False)
goal = derive_goal(ScenarioSpec(
    title="Launch Minecraft Dungeons and reach the main menu",
    intent="Launch the game with the gamepad and reach its own main menu."),
    settings)
goal.target = "Minecraft Dungeons"


def transition(before: ScreenType, after: ScreenType,
               klass: TransitionClass) -> Transition:
    return Transition(state_before=before, state_after=after,
                      classification=klass,
                      goal_complete=goal.is_success(
                          GameState(screen_type=after)),
                      action=Action(type=ActionType.PRESS, control="a"))


print("\nREPLAYING THE FAILED RUN'S STATE SEQUENCE")

# -- hop 1: navigate onto the tile ------------------------------------
t1 = transition(ScreenType.XCLOUD_HOME, ScreenType.GAME_FOCUSED,
                TransitionClass.SUCCESS)
check(route_after_verify({"last_transition": t1}) == "goal_check",
      "XCLOUD_HOME -> GAME_FOCUSED stays in the loop")

# -- hop 2: THE ONE THAT USED TO FAIL ---------------------------------
# A on the focused tile went straight to a fullscreen handoff instead of the
# predicted detail page. This is the exact moment the old run broke.
t2 = transition(ScreenType.GAME_FOCUSED, ScreenType.FULLSCREEN_TRANSITION,
                TransitionClass.INTERMEDIATE)
route2 = route_after_verify({"last_transition": t2})
check(route2 == "goal_check",
      "GAME_FOCUSED -> FULLSCREEN_TRANSITION stays in the loop")
check(route2 != "recover",
      "  ...and does NOT divert to recovery")
check(route2 != "rca",
      "  ...and does NOT reach root-cause analysis")

# -- hop 3: loading -------------------------------------------------
t3 = transition(ScreenType.FULLSCREEN_TRANSITION, ScreenType.GAME_LOADING,
                TransitionClass.INTERMEDIATE)
check(route_after_verify({"last_transition": t3}) == "goal_check",
      "FULLSCREEN_TRANSITION -> GAME_LOADING stays in the loop")

# -- hop 4: still loading is still fine ------------------------------
t4 = transition(ScreenType.GAME_LOADING, ScreenType.GAME_LOADING,
                TransitionClass.INTERMEDIATE)
check(route_after_verify({"last_transition": t4}) == "goal_check",
      "GAME_LOADING -> GAME_LOADING (no change yet) stays in the loop")

# -- hop 5: the splash ----------------------------------------------
t5 = transition(ScreenType.GAME_LOADING, ScreenType.GAME_SPLASH,
                TransitionClass.INTERMEDIATE)
check(route_after_verify({"last_transition": t5}) == "goal_check",
      "GAME_LOADING -> GAME_SPLASH stays in the loop")

# -- hop 6: arrival -------------------------------------------------
t6 = transition(ScreenType.PRESS_ANY_BUTTON, ScreenType.GAME_MAIN_MENU,
                TransitionClass.SUCCESS)
check(t6.goal_complete, "reaching GAME_MAIN_MENU sets goal_complete")
check(route_after_verify({"last_transition": t6}) == "evaluate",
      "GAME_MAIN_MENU -> evaluate (the run ends by ARRIVING)")


print("\nA REAL FAILURE STILL LEAVES THE LOOP")

silent = Transition(state_before=ScreenType.XCLOUD_HOME,
                    state_after=ScreenType.XCLOUD_HOME,
                    classification=TransitionClass.FAILURE,
                    silent_failure=True)
check(route_after_verify({"last_transition": silent}) == "recover",
      "a silent failure routes to recovery")

fatal = transition(ScreenType.GAME_LOADING, ScreenType.STREAM_ERROR,
                   TransitionClass.FAILURE)
check(route_after_verify({"last_transition": fatal}) == "recover",
      "a stream error routes to recovery (which will refuse and call RCA)")

check(route_after_verify({"halt_reason": "budget spent",
                          "last_transition": t1}) == "report",
      "a halt always reaches the report")


print("\nTHE LOOP TERMINATES")

check(route_after_goal_check({"goal_complete": True}) == "evaluate",
      "goal reached -> evaluate")
check(route_after_goal_check({"goal_complete": False}) == "decide",
      "goal not reached -> decide the next single action")
check(route_after_goal_check({"halt_reason": "iteration limit"}) == "report",
      "out of iterations -> report, never an infinite loop")


failed = [r for r in results if not r[0]]
print("\n" + "=" * 74)
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    for _, name in failed:
        print(f"  - {name}")
    sys.exit(1)
print("The launch sequence that used to fail at hop 2 now runs to completion.")
print("=" * 74)
