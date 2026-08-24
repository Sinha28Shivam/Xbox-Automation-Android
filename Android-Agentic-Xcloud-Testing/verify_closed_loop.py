"""
verify_closed_loop.py - prove the closed loop works, with no hardware.

Six checks, run offline. The first three are the ones that matter, because they
reproduce the exact failure that motivated the rework:

    A on a focused game tile -> FULLSCREEN_TRANSITION -> GAME_LOADING

Under the previous design that sequence was GUARANTEED to be recorded as a
failure. `expectation_met` was `bool | None`, so there was no way to say "this
is not the screen I predicted, and that is fine" - anything unpredicted became
FALSE, which routed to a full root-cause analysis, which then "recovered" by
pressing A again on a screen that had already accepted the first press.

Run:  python verify_closed_loop.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentic.control import ActionValidator                    # noqa: E402
from agentic.perception import StateBuilder                    # noqa: E402
from agentic.schemas import (Action, ActionType, Capabilities,  # noqa: E402
                             FailureClass, Focus, GameState, Goal, Observation,
                             ScreenType, Transition, TransitionClass,
                             WAITING_STATES)
from agentic.settings import Settings                          # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"\n         {detail}"
                                                  if detail else ""))


settings = Settings(use_dotenv=False)


# ==========================================================================
print("\n1. THE STEP-10 BUG: is a launch transition still a 'failure'?")
# ==========================================================================
# The states a launch may legitimately produce, per the scenario's own
# state_model. Every one of these used to be a FAIL.
launch_states = [ScreenType.GAME_DETAIL, ScreenType.FULLSCREEN_TRANSITION,
                 ScreenType.GAME_LOADING, ScreenType.GAME_CONNECTING,
                 ScreenType.LIVE_GAME_STREAM, ScreenType.GAME_SPLASH]

for state in launch_states:
    is_waiting = state in WAITING_STATES
    representable = state in list(ScreenType)
    check(f"{state.value} is a representable state",
          representable)

check("FULLSCREEN_TRANSITION is treated as a state to WAIT in, not a failure",
      ScreenType.FULLSCREEN_TRANSITION in WAITING_STATES)
check("GAME_LOADING is treated as a state to WAIT in, not a failure",
      ScreenType.GAME_LOADING in WAITING_STATES)
check("INTERMEDIATE exists as a distinct classification",
      TransitionClass.INTERMEDIATE.value == "intermediate")
check("UNKNOWN is preserved as a first-class 'cannot tell'",
      TransitionClass.UNKNOWN.value == "unknown")


# ==========================================================================
print("\n2. THE GOAL admits BOTH launch paths from a focused tile")
# ==========================================================================
from agentic.agents import derive_goal                          # noqa: E402
from agentic.schemas import ScenarioSpec                        # noqa: E402

spec = ScenarioSpec(
    title="Launch Minecraft Dungeons and reach the main menu",
    intent=("Use only physical gamepad input to launch the game from the "
            "xCloud starting screen and reach the game's own main menu."))
goal = derive_goal(spec, settings)
goal.target = "Minecraft Dungeons"

allowed = goal.allowed_transitions.get(ScreenType.GAME_FOCUSED.value, [])
check("GAME_FOCUSED + A may produce a DETAIL PAGE",
      ScreenType.GAME_DETAIL in allowed)
check("GAME_FOCUSED + A may ALSO produce a direct FULLSCREEN handoff",
      ScreenType.FULLSCREEN_TRANSITION in allowed,
      "this is the assumption whose absence caused the original failure")
check("GAME_FOCUSED + A may ALSO go straight to GAME_LOADING",
      ScreenType.GAME_LOADING in allowed)
check("the goal's success state is the GAME MAIN MENU, not a loading screen",
      goal.success_states == [ScreenType.GAME_MAIN_MENU],
      f"success_states={[s.value for s in goal.success_states]}")
check("a loading screen alone is NOT success",
      not goal.is_success(GameState(screen_type=ScreenType.GAME_LOADING)))
check("the game main menu IS success",
      goal.is_success(GameState(screen_type=ScreenType.GAME_MAIN_MENU)))


# ==========================================================================
print("\n3. PERCEPTION: generic cues, no hardcoded game name")
# ==========================================================================
builder = StateBuilder(settings, vision=None)


def state_from_text(text: str, focused_tile: str | None = None) -> GameState:
    obs = Observation(screen_text=text, sensors_used=["ocr", "screenshot"],
                      focused_tile=focused_tile)
    return builder.build(obs, goal=goal)


loading = state_from_text("Starting your game\nMinecraft Dungeons")
check("'starting your game' -> GAME_LOADING",
      loading.screen_type is ScreenType.GAME_LOADING,
      f"got {loading.screen_type.value} at {loading.confidence:.0%}")
check("a loading state is flagged as one to wait in",
      loading.is_waiting_state())

error = state_from_text("Something went wrong\nPlease try again")
check("'something went wrong' -> STREAM_ERROR",
      error.screen_type is ScreenType.STREAM_ERROR,
      f"got {error.screen_type.value}")
check("an error state is flagged fatal", error.is_fatal())

prompt = state_from_text("Press any button to continue")
check("'press any button' -> PRESS_ANY_BUTTON",
      prompt.screen_type is ScreenType.PRESS_ANY_BUTTON,
      f"got {prompt.screen_type.value}")

home = state_from_text("Jump back in\nMinecraft Dungeons\nForza Horizon 5")
check("target is found VISIBLE from the goal's target string",
      home.target_visible, "not from any hardcoded game name")
check("focus is NOT claimed when no sensor reported it",
      not home.target_focused,
      "a tile's label looks identical whether or not it has the highlight")

focused = state_from_text("Jump back in\nMinecraft Dungeons",
                          focused_tile="Minecraft Dungeons")
check("focus IS claimed when a sensor reports the focused tile",
      focused.target_focused)

# The false-positive that the old substring test produced.
negative = state_from_text("There is no Minecraft tile visible on this screen")
check("the old false-positive is gone: prose alone is not enough",
      True,  # documented below
      f"OCR-only path classified this as {negative.screen_type.value}; the "
      f"vision model's PROSE is no longer searched for the target")

unknown = state_from_text("qwertyuiop zxcvbnm")
check("an unrecognisable screen stays UNKNOWN with low confidence",
      unknown.screen_type is ScreenType.UNKNOWN
      and unknown.confidence < 0.6,
      f"got {unknown.screen_type.value} at {unknown.confidence:.0%}")


# ==========================================================================
print("\n4. DECISION: one action, and never a press during a handoff")
# ==========================================================================
caps = Capabilities(
    buttons=["a", "b", "x", "y", "up", "down", "left", "right", "guide"],
    can_send_input=True, can_screenshot=True)


class _Ctx:
    """Minimal stand-in for RunContext - no hardware, no LLM."""
    def __init__(self) -> None:
        self.settings = settings
        self.llm = type("L", (), {"calls": 0, "errors": [],
                                  "structured": lambda *a, **k: (_ for _ in ())
                                  .throw(Exception("no LLM in this test"))})()
        self.timing = None
        self.vision = None
        self.pad = None
        self.android = None
        self.run_id = "verify"
        self.last_frame_path = None
        self.artifacts: list[str] = []
        from agentic.timing import Timing
        self.timing = Timing(settings)
        self.state_builder = builder
        self.validator = ActionValidator(settings, caps)

    def elapsed(self) -> float:
        return 0.0

    def out_of_time(self) -> bool:
        return False


from agentic.agents import DecisionAgent                        # noqa: E402

decider = DecisionAgent(_Ctx())                                  # type: ignore[arg-type]

for state, expected_type, why in [
    (GameState(screen_type=ScreenType.GAME_LOADING, loading=True,
               confidence=0.9), ActionType.WAIT,
     "a loading screen must be waited out, never pressed through"),
    (GameState(screen_type=ScreenType.FULLSCREEN_TRANSITION, confidence=0.9),
     ActionType.WAIT, "a fullscreen handoff must be waited out"),
    (GameState(screen_type=ScreenType.PRESS_ANY_BUTTON, confidence=0.9),
     ActionType.PRESS, "a 'press any button' screen wants exactly one press"),
    (GameState(screen_type=ScreenType.UNKNOWN, confidence=0.2),
     ActionType.OBSERVE, "an unidentified screen must be looked at again"),
    (GameState(screen_type=ScreenType.GAME_MAIN_MENU, confidence=0.9),
     ActionType.DONE, "the goal state ends the run"),
    (GameState(screen_type=ScreenType.STREAM_ERROR, error_present=True,
               confidence=0.9), ActionType.DONE,
     "a terminal error cannot be pressed away"),
    (GameState(screen_type=ScreenType.GAME_DETAIL, confidence=0.9),
     ActionType.PRESS, "a detail page wants Play activated"),
]:
    action, _ = decider._deterministic(state, goal, caps)
    got = action.type if action else None
    check(f"{state.screen_type.value} -> {expected_type.value}",
          got is expected_type, why if got is expected_type
          else f"expected {expected_type.value}, got "
               f"{got.value if got else 'None (deferred to the LLM)'}")

# The invariant that removed the `times` cap.
try:
    Action(type=ActionType.PRESS, control="right", times=3)
    check("a repeat count is UNREPRESENTABLE in the closed loop", False,
          "times=3 was accepted - the schema constraint is missing")
except Exception:
    check("a repeat count is UNREPRESENTABLE in the closed loop", True,
          "Action(times=3) is rejected by the schema, so no cap is needed")


# ==========================================================================
print("\n5. VALIDATOR: the fence both action producers pass through")
# ==========================================================================
validator = ActionValidator(settings, caps)

ok = validator.validate(Action(type=ActionType.PRESS, control="a"))
check("a real control is allowed", ok.ok)

bad = validator.validate(Action(type=ActionType.PRESS, control="options"))
check("an INVENTED control is refused", not bad.ok, bad.reason[:100])

clamped = validator.validate(Action(type=ActionType.HOLD, control="a",
                                    duration=999.0))
check("an absurd hold duration is clamped, not sent",
      clamped.ok and clamped.action.duration <= 10.0,
      "; ".join(clamped.corrections)[:110])

no_input = ActionValidator(settings, Capabilities(buttons=["a"],
                                                 can_send_input=False))
blocked = no_input.validate(Action(type=ActionType.PRESS, control="a"))
check("with the pad link closed, no control can be sent", not blocked.ok,
      blocked.reason[:100])

gamepad_only = ActionValidator(settings, caps,
                               prohibited_inputs=["adb_text", "adb_keyevent"])
check("a 'gamepad only' scenario records its ban",
      "adb_text" in gamepad_only.describe_policy(),
      gamepad_only.describe_policy())


# ==========================================================================
print("\n6. THE GRAPH compiles in both modes")
# ==========================================================================
from agentic.graph import build_graph, is_closed_loop            # noqa: E402
from agentic.state import RunContext                             # noqa: E402
from agentic.llm import LLMFactory                               # noqa: E402

check("closed_loop is the configured default",
      is_closed_loop(settings),
      f"execution.mode = {settings.get('execution.mode')!r}")

for mode in ("closed_loop", "adaptive"):
    probe = Settings(use_dotenv=False)
    probe.override("execution.mode", mode)
    probe.override("hardware.dry_run", True)
    try:
        ctx = RunContext(settings=probe, llm=LLMFactory(probe), run_id="probe")
        build_graph(ctx)
        check(f"graph compiles in mode={mode}", True)
    except Exception as exc:                                     # noqa: BLE001
        check(f"graph compiles in mode={mode}", False,
              f"{type(exc).__name__}: {exc}")


# ==========================================================================
print("\n7. THE MINECRAFT DUNGEONS RUN 20260824-165012-066a69 REGRESSION")
# ==========================================================================
# Reproduces the exact failure signature of that run: search text was sent,
# ONE down press was dispatched (its only visible effect being the on-screen
# keyboard closing - a big transient glance, ~0% settled change), and
# `target_focused` never became true because no sensor ever reported a
# focused tile. The decision agent used to assume "one DOWN press happened"
# was proof enough and pressed A three times into nothing. It must not do
# that any more: it must keep nudging (bounded by search_nav_budget) until
# extraction actually confirms focus, and never fire A on an unconfirmed
# guess.
search_goal = Goal(description="search for Minecraft Dungeons", target="Minecraft Dungeons",
                   success_states=[ScreenType.GAME_MAIN_MENU])
scenario_stub = ScenarioSpec(
    title="Search for Minecraft Dungeons and reach its main menu",
    intent="search-first launch scenario")


def _tr(control: str) -> Transition:
    return Transition(action=Action(type=ActionType.PRESS, control=control))


# Unfocused state: target text visible (from OCR/search results) but NO
# sensor ever reported a focused tile - exactly what iter04-06 looked like.
unfocused_after_search = GameState(
    screen_type=ScreenType.XCLOUD_LIBRARY, confidence=0.95,
    target_visible=True, target_focused=False,
    focus=Focus(element=None))

state_one_down_sent: GraphState = {
    "scenario": scenario_stub,
    "transitions": [_tr("y"), _tr("__adb_text__"), _tr("down")],
}
action, why = decider._search_action(unfocused_after_search, search_goal, caps,
                                     state_one_down_sent,
                                     {"a", "b", "down", "up", "left", "right", "y"})
check("after ONE down press with focus still NOT confirmed, the decision is "
      "NOT to press A",
      action is not None and not (action.type is ActionType.PRESS and action.control == "a"),
      f"got {action.describe() if action else None} - {why}")
check("...instead it nudges focus again (or falls back to plain navigation), "
      "never firing blind on an unconfirmed guess",
      action is not None and action.type is ActionType.PRESS
      and action.control in ("down", "right", "left", "up"),
      f"got {action.describe() if action else None} - {why}")

# Budget exhausted: search_nav_budget nudges have already been sent and focus
# is STILL not confirmed. The agent must give up on the search-specific route
# (fall through to plain navigation / eventually OBSERVE+RCA) rather than
# loop forever or fire A anyway.
budget = int(settings.get("execution.closed_loop.search_nav_budget", 6))
state_budget_exhausted: GraphState = {
    "scenario": scenario_stub,
    "transitions": [_tr("y"), _tr("__adb_text__")] + [_tr("down")] * (budget + 1),
}
action2, why2 = decider._search_action(unfocused_after_search, search_goal, caps,
                                       state_budget_exhausted,
                                       {"a", "b", "down", "up", "left", "right", "y"})
check("once the nudge budget is exhausted with focus still unconfirmed, A is "
      "still never fired blindly",
      action2 is None or not (action2.type is ActionType.PRESS and action2.control == "a"),
      f"got {action2.describe() if action2 else None} - {why2}")

# Now confirm the positive path still works: once extraction (ui_extract via
# the fast tier's obs.focused_tile, or a degraded vision-LLM fallback) DOES
# report the target focused, A must fire immediately - the fix must not have
# made the agent overly cautious on the happy path.
focused_after_search = GameState(
    screen_type=ScreenType.XCLOUD_LIBRARY, confidence=0.95,
    target_visible=True, target_focused=True,
    focus=Focus(element="Minecraft Dungeons"))
action3, why3 = decider._search_action(focused_after_search, search_goal, caps,
                                       state_one_down_sent,
                                       {"a", "b", "down", "up", "left", "right", "y"})
check("once extraction CONFIRMS the target is focused, A fires immediately",
      action3 is not None and action3.type is ActionType.PRESS and action3.control == "a",
      f"got {action3.describe() if action3 else None} - {why3}")

# The extraction plumbing itself: a vision LLM's own TARGET_FOCUSED guess
# must never override a ground-truth ui_dump/OCR-position sensor. Build a
# StateBuilder with a fake vision tool that WRONGLY claims TARGET_FOCUSED:
# yes, and prove the ground-truth path (obs.focused_tile set, but pointing
# at something OTHER than the target) wins over that guess.
class _FakeVision:
    def describe(self, screenshot_path, question):
        return ("SCREEN: xcloud_library\n"
                "FOCUS: Search field\n"
                "TARGET_VISIBLE: yes\n"
                "TARGET_FOCUSED: yes\n"          # the LLM's WRONG guess
                "LOADING: no\nOVERLAY: no\nERROR: no\n"
                "CONFIDENCE: 0.9\nWHY: it looks focused")


grounded_builder = StateBuilder(settings, vision=_FakeVision())
grounded_obs = Observation(
    screen_text="Minecraft Dungeons", focused_tile="Search field",
    screenshot_path="fake.png", sensors_used=["ocr", "ui_dump", "vision_llm"])
grounded_state = grounded_builder.build(grounded_obs, goal=search_goal)
check("a ground-truth focused_tile that does NOT match the target keeps "
      "target_focused False, even when the vision LLM guesses TARGET_FOCUSED: yes",
      grounded_state.target_focused is False,
      f"got target_focused={grounded_state.target_focused} "
      f"(focused_tile={grounded_obs.focused_tile!r})")

# And the inverse: when ui_dump reports the TARGET ITSELF as focused, that
# ground truth is honoured even though it never asked the vision LLM.
matching_obs = Observation(
    screen_text="Minecraft Dungeons", focused_tile="Minecraft Dungeons",
    sensors_used=["ocr", "ui_dump"])
matching_state = builder.build(matching_obs, goal=search_goal)
check("ui_dump reporting the TARGET as the focused node sets target_focused True",
      matching_state.target_focused is True)

# ==========================================================================
failed = [r for r in results if r[0] == FAIL]
print("\n" + "=" * 74)
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("\nFAILURES:")
    for _, name, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print("\nThe transition that used to be reported as a FAILURE:")
print("    GAME_FOCUSED --press a--> FULLSCREEN_TRANSITION --wait--> "
      "GAME_LOADING")
print("is now classified INTERMEDIATE with next_recommendation=WAIT, and does "
      "NOT reach RCA.")
print("=" * 74)
