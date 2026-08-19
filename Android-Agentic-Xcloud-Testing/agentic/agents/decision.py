"""
decision.py - choose ONE action from the state in front of us.

This is the agent that did not exist before, and its absence was the single
structural fault in the old design. Previously the PlannerAgent decided every
action BEFORE the first screenshot was taken, and the executor then walked a
cursor through that list. Feedback reached the graph's ROUTER (continue, or
divert to RCA) but never reached action SELECTION - so the observed screen could
not change what was pressed next, only whether the run continued.

Two pieces of code in the old executor were the visible symptoms:

  * a cap that rewrote `times=3` to `times=1` for D-pad presses, needed only
    because the planner had to guess a repeat count in advance
  * a scan of FUTURE plan steps, string-matching their `intent` prose for
    "play"/"launch"/"stream" to guess where to jump when a detail page appeared

Both were a state machine being emulated with string search. With a decision
made per observation, neither is expressible: the number of presses is an
OUTPUT of watching where the focus went.

CHEAP DECISIONS ARE MADE IN CODE, NOT BY THE MODEL
--------------------------------------------------
`_deterministic` runs first and answers the majority of real situations with no
LLM call at all:

    loading / transition / queue  -> WAIT      (never press during a handoff)
    "press any button"            -> PRESS a
    a dialog or overlay           -> PRESS b   (dismiss what steals input)
    low perception confidence     -> OBSERVE   (look again before acting)
    the goal state                -> DONE

That list is not an optimisation bolted on afterwards - it is the correct
behaviour, and expressing it as code makes it auditable and free. The LLM is
asked only for the genuinely open question: which way to move through a menu we
have not seen before.

THE HONESTY RULE STILL APPLIES
------------------------------
When the state is UNKNOWN the answer is OBSERVE, never a hopeful button press.
Pressing something to "see what happens" on an unidentified screen is how a run
ends up in a submenu it cannot name, and every observation after that is
evidence about the wrong thing.
"""

from __future__ import annotations

from ..logbook import log
from ..schemas import (Action, ActionType, Capabilities, GameState, Goal,
                       ScreenType, Transition, WAITING_STATES)
from ..state import GraphState
from .base import Agent

ROLE = """\
You choose exactly ONE gamepad action, given the CURRENT screen state and the
goal. You will be called again after the action is executed and observed.

Hard rules:
1. ONE action. Never a sequence. You will see the result and decide again.
2. `control` must be a name from the capability list, spelled exactly as shown.
   Never invent a control.
3. Navigate ONE step at a time. Do not ask for a repeat count: after a single
   directional press you will be shown where the highlight actually went, which
   is the only reliable way to know how many more are needed.
4. If the screen is loading, connecting, mid-transition or queueing, answer WAIT.
   Pressing a button during a handoff either does nothing or acts on the screen
   that arrives next, which is worse.
5. If you cannot tell what the screen is (UNKNOWN), answer OBSERVE. A guess on
   an unidentified screen can move the run somewhere it cannot recover from.
   HOWEVER, if the screen IS identified (such as xcloud_home or xcloud_library)
   and stable, DO NOT repeatedly OBSERVE: if no element currently holds focus,
   send a directional press (e.g. `down` or `right`) to place the initial focus
   on the grid and begin moving toward the target.
6. If the goal state has been reached, answer DONE.
7. Fill `expected_states` with EVERY state this action could legitimately
   produce, not just the one you consider most likely. Selecting a game may open
   a detail page OR go straight to a fullscreen handoff; listing both is what
   allows a correct launch to be recognised instead of being called a failure.
8. `rationale` must cite what you can see in the state - "the target is visible
   but the highlight is on something else, so move toward it", not "navigating".
"""


class DecisionAgent(Agent):
    name = "decision"

    def run(self, state: GraphState) -> GraphState:
        game_state: GameState | None = state.get("game_state")
        goal: Goal | None = state.get("goal")
        caps: Capabilities | None = state.get("capabilities")

        if game_state is None or goal is None:
            return {
                "halt_reason": "the decision agent ran before a state or goal "
                               "existed - this is a harness bug",
                "agent_trace": [self.trace("decide", "missing inputs")],
            }

        # -- 1. the cheap, correct answers ------------------------------
        action, why = self._deterministic(game_state, goal, caps)
        if action is not None:
            log.act(f"decided (no LLM): {action.describe()} - {why}", indent=1)
            return {
                "pending_action": action,
                "agent_trace": [self.trace(
                    "decide", f"{action.describe()} [deterministic] - {why}")],
            }

        # -- 2. the open question, for the model ------------------------
        decided: Action | None = self.think(
            Action, self.system_prompt(ROLE),
            self._prompt(state, game_state, goal), default=None)

        if decided is None:
            decided = self._fallback(game_state, goal, caps)
            log.act(f"decided (mechanical fallback): {decided.describe()} - "
                    f"{decided.rationale}", indent=1)
        else:
            log.act(f"decided: {decided.describe()} - {decided.rationale}",
                    indent=1)

        return {
            "pending_action": decided,
            "agent_trace": [self.trace(
                "decide", f"{decided.describe()} - {decided.rationale[:120]}")],
        }

    # ------------------------------------------------------------------
    # Deterministic policy - runs before any LLM call
    # ------------------------------------------------------------------
    def _deterministic(self, gs: GameState, goal: Goal,
                       caps: Capabilities | None
                       ) -> tuple[Action | None, str]:
        """The situations where the right action is not a matter of opinion.

        Returns (None, "") when the case is genuinely open and the model should
        be asked.
        """
        buttons = {b.lower() for b in (caps.buttons if caps else [])}

        # -- the goal ---------------------------------------------------
        if goal.is_success(gs):
            return (Action(type=ActionType.DONE,
                           rationale=(f"the goal state "
                                      f"{gs.screen_type.value} has been "
                                      f"reached"),
                           expected_states=[gs.screen_type]),
                    "goal state reached")

        # -- fatal ------------------------------------------------------
        if gs.is_fatal() or goal.is_failure(gs):
            # Deliberately DONE rather than a retry. A stream error or an
            # expired session will not be fixed by another button press, and
            # continuing to press would bury the evidence of what went wrong.
            return (Action(type=ActionType.DONE,
                           rationale=(f"{gs.screen_type.value} is a terminal "
                                      f"failure state; no controller action "
                                      f"can recover it"),
                           expected_states=[gs.screen_type]),
                    "terminal failure state")

        # -- perception too weak to act on ------------------------------
        threshold = float(
            self.s.get("execution.closed_loop.confidence.reobserve", 0.60))
        if gs.screen_type is ScreenType.UNKNOWN or gs.confidence < threshold:
            return (Action(type=ActionType.OBSERVE,
                           rationale=(f"the screen is "
                                      f"{gs.screen_type.value} at only "
                                      f"{gs.confidence:.0%} confidence, which "
                                      f"is below the {threshold:.0%} needed to "
                                      f"act - look again rather than guess"),
                           expected_states=[]),
                    f"confidence {gs.confidence:.0%} below {threshold:.0%}")

        # -- the page has LOST the pad ----------------------------------
        #
        # A "connect a controller" prompt mid-run means the page is no longer
        # receiving reports - the tab reloaded, the stream handed off, or the pad
        # went idle long enough to be dropped. The fix is the same handshake the
        # bootstrap performs, and it must be a MACRO rather than a plain press:
        # the point is a sequence the page will notice, not one more button that
        # the prompt itself will swallow.
        #
        # Checked BEFORE the waiting rules, deliberately. A controller prompt on
        # top of a loading screen still needs answering, and waiting for a screen
        # that is itself waiting for US is a deadlock the run cannot leave.
        if (gs.screen_type is ScreenType.CONTROLLER_PROMPT
                or gs.controller_prompt):
            if "signal_handshake" in (caps.special_actions if caps else {}):
                return (Action(type=ActionType.MACRO,
                               control="signal_handshake",
                               rationale=("the page is asking for a controller, "
                                          "so it has stopped seeing the pad - "
                                          "re-run the signal handshake to make "
                                          "the gamepad visible again"),
                               expected_states=[ScreenType.XCLOUD_HOME,
                                                ScreenType.GAME_FOCUSED,
                                                ScreenType.LIVE_GAME_STREAM,
                                                ScreenType.IN_GAME]),
                        "controller prompt - re-handshake")
            if "guide" in buttons:
                return (Action(type=ActionType.PRESS, control="guide",
                               rationale=("the page is asking for a controller; "
                                          "Guide is the most visible button "
                                          "report available to announce the pad"),
                               expected_states=[ScreenType.XCLOUD_HOME,
                                                ScreenType.OVERLAY,
                                                ScreenType.GAME_FOCUSED]),
                        "controller prompt - announce with Guide")

        # -- anything mid-flight: WAIT ----------------------------------
        #
        # This is the rule whose absence caused a successful launch to be
        # recorded as a failure and then to be "recovered" by pressing A again,
        # on a screen that had already accepted the first press.
        if gs.screen_type in WAITING_STATES or gs.loading:

            seconds = self._wait_seconds(gs, caps)
            return (Action(type=ActionType.WAIT, seconds=seconds,
                           rationale=(f"{gs.screen_type.value} is a transient "
                                      f"state the app is working through; "
                                      f"waiting {seconds:.1f}s and looking "
                                      f"again is correct, and pressing "
                                      f"anything now would act on whatever "
                                      f"screen arrives next"),
                           expected_states=list(WAITING_STATES) + [
                               ScreenType.LIVE_GAME_STREAM,
                               ScreenType.GAME_SPLASH,
                               ScreenType.PRESS_ANY_BUTTON,
                               ScreenType.GAME_MAIN_MENU]),
                    f"{gs.screen_type.value} needs time, not input")

        # -- "press any button" -----------------------------------------
        if gs.screen_type is ScreenType.PRESS_ANY_BUTTON and "a" in buttons:
            return (Action(type=ActionType.PRESS, control="a",

                           rationale="a 'press any button' screen is asking "
                                     "for exactly one input to continue",
                           expected_states=[ScreenType.GAME_MAIN_MENU,
                                            ScreenType.GAME_SPLASH,
                                            ScreenType.IN_GAME]),
                    "press-any-button prompt")

        # -- overlays that steal input ----------------------------------
        if (gs.screen_type in (ScreenType.DIALOG, ScreenType.OVERLAY)
                or gs.overlay_present) and "b" in buttons:
            return (Action(type=ActionType.PRESS, control="b",
                           rationale=("a dialog or overlay is present and will "
                                      "consume gamepad input aimed at the "
                                      "screen behind it, so dismiss it first"),
                           expected_states=[ScreenType.XCLOUD_HOME,
                                            ScreenType.GAME_FOCUSED,
                                            ScreenType.GAME_DETAIL,
                                            ScreenType.LIVE_GAME_STREAM]),
                    "dismiss overlay before doing anything else")

        # -- the target is focused: select it ---------------------------
        if gs.target_focused and "a" in buttons:
            return (Action(type=ActionType.PRESS, control="a",
                           rationale=(f"the target {goal.target!r} holds the "
                                      f"highlight, so select it"),
                           # BOTH launch paths are declared legitimate. This is
                           # the fix for the original failure: a tile may open a
                           # detail page or hand straight off to fullscreen, and
                           # a rig that admits only one will call the other a
                           # failure.
                           expected_states=[ScreenType.GAME_DETAIL,
                                            ScreenType.FULLSCREEN_TRANSITION,
                                            ScreenType.GAME_LOADING,
                                            ScreenType.GAME_CONNECTING,
                                            ScreenType.LIVE_GAME_STREAM,
                                            ScreenType.GAME_SPLASH]),
                    "target focused - select it")

        # -- a detail page with Play on it ------------------------------
        if gs.screen_type is ScreenType.GAME_DETAIL and "a" in buttons:
            return (Action(type=ActionType.PRESS, control="a",
                           rationale=("the detail page is open; activating the "
                                      "default Play action starts the stream"),
                           expected_states=[ScreenType.FULLSCREEN_TRANSITION,
                                            ScreenType.GAME_LOADING,
                                            ScreenType.GAME_CONNECTING,
                                            ScreenType.LIVE_GAME_STREAM]),
                    "detail page open - activate Play")

        # Everything else is a real navigation question. Ask the model.
        return None, ""

    def _wait_seconds(self, gs: GameState, caps: Capabilities | None) -> float:
        """How long to wait in a transient state.

        Read from the rig's own controls.yaml timing where possible, because a
        slower phone should be a YAML edit and not a code change. Deliberately
        state-dependent rather than one global number: a fullscreen handoff is
        a second or two, a game boot is tens of seconds, and using one value for
        both either wastes the run or photographs the wrong moment.
        """
        timing = (caps.timing if caps else {}) or {}
        if gs.screen_type in (ScreenType.GAME_SPLASH,):
            return float(timing.get("game_boot_wait", 8.0))
        if gs.screen_type in (ScreenType.QUEUE, ScreenType.NETWORK_WAIT):
            return float(timing.get("stream_start_wait", 10.0))
        if gs.screen_type in (ScreenType.GAME_LOADING,
                              ScreenType.GAME_CONNECTING):
            return float(timing.get("loading_observation_interval", 3.0))
        return float(timing.get("menu_transition_wait", 2.0))

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------
    def _prompt(self, state: GraphState, gs: GameState, goal: Goal) -> str:
        parts = [
            "THE GOAL",
            f"  {goal.description or 'reach the target state'}",
            f"  target: {goal.target or 'not specified'}",
            "  success states: " + (", ".join(
                s.value for s in goal.success_states) or "not specified"),
            "",
            "THE SCREEN RIGHT NOW",
            f"  {gs.summary()}",
        ]
        if gs.focus.element:
            parts.append(f"  the highlight is on: {gs.focus.element!r}")
        else:
            parts.append("  no element currently has focus (press a direction such as "
                         "'down' or 'right' to place focus on the page and start navigation)")
        if gs.visible_text:
            parts.append("  text visible on screen:")
            parts += [f"    {line}" for line in gs.visible_text[:25]]
        if gs.evidence:
            parts.append("  why the screen was classified this way:")
            parts += [f"    - {e}" for e in gs.evidence[:6]]

        parts += ["", self.capability_block(state)]

        history = self._history(state)
        if history:
            parts += ["", "WHAT HAS ALREADY BEEN TRIED (most recent last)",
                      history,
                      "",
                      "Do not repeat an action that produced no change twice "
                      "in a row - if the last identical action did nothing, "
                      "something is consuming the input and a different action "
                      "is needed."]
        return "\n".join(parts)

    @staticmethod
    def _history(state: GraphState, limit: int = 6) -> str:
        """The recent transitions, so the model can avoid repeating itself.

        Short on purpose. The full run history would crowd out the current
        state, and the decision only needs to know what was just tried and
        whether it worked.
        """
        transitions: list[Transition] = list(state.get("transitions", []))
        if not transitions:
            return ""
        lines = []
        for item in transitions[-limit:]:
            lines.append(f"  {item.describe()}"
                         + (f" ({item.failure_class.value})"
                            if item.failure_class.value != "none" else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # No-LLM fallback
    # ------------------------------------------------------------------
    def _fallback(self, gs: GameState, goal: Goal,
                  caps: Capabilities | None) -> Action:
        """A rule table for when no model is available.

        Weaker than the LLM path and labelled as such, but never absent - the
        project's standing rule is that a missing API key degrades a run, it
        does not end one. It can navigate toward a visible target and select it,
        which is enough to exercise the whole hardware path.
        """
        buttons = {b.lower() for b in (caps.buttons if caps else [])}

        if gs.target_visible and not gs.target_focused and "right" in buttons:
            return Action(
                type=ActionType.PRESS, control="right",
                rationale=("no LLM available. The target is visible but not "
                           "focused, so step the highlight one place toward it "
                           "and look again"),
                expected_states=[ScreenType.GAME_FOCUSED,
                                 ScreenType.XCLOUD_HOME])

        if gs.screen_type in (ScreenType.XCLOUD_HOME,
                             ScreenType.XCLOUD_LIBRARY) and "right" in buttons:
            return Action(
                type=ActionType.PRESS, control="right",
                rationale=("no LLM available. Explore the rail one step at a "
                           "time to find the target"),
                expected_states=[ScreenType.XCLOUD_HOME,
                                 ScreenType.GAME_FOCUSED])

        return Action(
            type=ActionType.OBSERVE,
            rationale=("no LLM available and no mechanical rule fits this "
                       "screen, so observe rather than press something "
                       "arbitrary"),
            expected_states=[])
