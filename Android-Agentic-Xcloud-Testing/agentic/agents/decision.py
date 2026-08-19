"""Closed-loop decision agent for xCloud."""

from __future__ import annotations

from ..logbook import log
from ..schemas import Action, ActionType, Capabilities, GameState, Goal, ScreenType, Transition, WAITING_STATES
from ..state import GraphState
from .base import Agent

ROLE = """\
Choose exactly ONE action from the current observed screen.
Use physical gamepad Y to open xCloud Search in search-first scenarios. Once the
search field is focused, use the ADB text fixture once, then use only physical
controller input for result selection and game launch.
"""


class DecisionAgent(Agent):
    name = "decision"

    def run(self, state: GraphState) -> GraphState:
        gs: GameState | None = state.get("game_state")
        goal: Goal | None = state.get("goal")
        caps: Capabilities | None = state.get("capabilities")
        if gs is None or goal is None:
            return {"halt_reason": "decision ran without state/goal",
                    "agent_trace": [self.trace("decide", "missing inputs")]}

        action, why = self._deterministic(gs, goal, caps, state)
        if action is None:
            action = self.think(Action, self.system_prompt(ROLE),
                                self._prompt(state, gs, goal), default=None)

        if action is not None and action.type is ActionType.OBSERVE:
            forced = self._force_progress(gs, goal, caps, state)
            if forced is not None:
                action, why = forced
                log.act(f"overrode LLM observe: {action.describe()} - {why}", indent=1)

        if action is None:
            action = self._fallback(gs, goal, caps, state)
            why = action.rationale

        log.act(f"decided: {action.describe()} - {why or action.rationale}", indent=1)
        return {"pending_action": action,
                "agent_trace": [self.trace("decide", f"{action.describe()} - {(why or action.rationale)[:180]}")]}

    def _deterministic(self, gs: GameState, goal: Goal,
                       caps: Capabilities | None, state: GraphState):
        buttons = {b.lower() for b in (caps.buttons if caps else [])}
        if goal.is_success(gs):
            return Action(type=ActionType.DONE,
                          rationale=f"goal state {gs.screen_type.value} reached",
                          expected_states=[gs.screen_type]), "goal reached"
        if gs.is_fatal() or goal.is_failure(gs):
            return Action(type=ActionType.DONE,
                          rationale=f"terminal state {gs.screen_type.value}",
                          expected_states=[gs.screen_type]), "terminal failure"

        threshold = float(self.s.get("execution.closed_loop.confidence.reobserve", 0.60))
        if gs.screen_type is ScreenType.UNKNOWN or gs.confidence < threshold:
            return Action(type=ActionType.OBSERVE,
                          rationale=f"screen confidence {gs.confidence:.0%} is below {threshold:.0%}",
                          expected_states=[]), "observe uncertain screen"

        if gs.screen_type is ScreenType.CONTROLLER_PROMPT or gs.controller_prompt:
            if "signal_handshake" in (caps.special_actions if caps else {}):
                return Action(type=ActionType.MACRO, control="signal_handshake",
                              rationale="controller prompt requires handshake",
                              expected_states=[ScreenType.XCLOUD_HOME]), "controller prompt"
            if "guide" in buttons:
                return Action(type=ActionType.PRESS, control="guide",
                              rationale="controller prompt requires Guide",
                              expected_states=[ScreenType.XCLOUD_HOME, ScreenType.GAME_FOCUSED]), "controller prompt"

        if gs.screen_type in WAITING_STATES or gs.loading:
            seconds = self._wait_seconds(gs, caps)
            return Action(type=ActionType.WAIT, seconds=seconds,
                          rationale=f"{gs.screen_type.value} is transient; wait",
                          expected_states=list(WAITING_STATES) + [ScreenType.LIVE_GAME_STREAM,
                          ScreenType.PRESS_ANY_BUTTON, ScreenType.GAME_MAIN_MENU]), "waiting state"

        if gs.screen_type is ScreenType.PRESS_ANY_BUTTON and "a" in buttons:
            return Action(type=ActionType.PRESS, control="a",
                          rationale="press-any-button screen explicitly requests input",
                          expected_states=[ScreenType.GAME_MAIN_MENU, ScreenType.GAME_SPLASH, ScreenType.IN_GAME]), "press-any-button"

        # SEARCH-FIRST MUST RUN BEFORE THE GENERIC OVERLAY/DOWN RULE.
        # On xCloud mobile, the Y search panel may be represented as an overlay
        # or keyboard state. B must never close it before the text fixture runs.
        if self._search_first(goal, state) and gs.screen_type in (
                ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                ScreenType.OVERLAY, ScreenType.KEYBOARD):
            search = self._search_action(gs, goal, caps, state, buttons)
            if search is not None:
                return search

        if (gs.screen_type in (ScreenType.DIALOG, ScreenType.OVERLAY) or gs.overlay_present) and "b" in buttons:
            return Action(type=ActionType.PRESS, control="b",
                          rationale="dismiss visible dialog/overlay",
                          expected_states=[ScreenType.XCLOUD_HOME, ScreenType.GAME_FOCUSED, ScreenType.GAME_DETAIL]), "dismiss overlay"

        if gs.target_focused and "a" in buttons:
            return Action(type=ActionType.PRESS, control="a",
                          rationale=f"target {goal.target!r} is focused; select it",
                          expected_states=[ScreenType.GAME_DETAIL, ScreenType.FULLSCREEN_TRANSITION,
                          ScreenType.GAME_LOADING, ScreenType.GAME_CONNECTING,
                          ScreenType.LIVE_GAME_STREAM, ScreenType.GAME_SPLASH]), "target focused"

        if gs.screen_type is ScreenType.GAME_DETAIL and "a" in buttons:
            return Action(type=ActionType.PRESS, control="a",
                          rationale="game detail page is open; activate Play",
                          expected_states=[ScreenType.FULLSCREEN_TRANSITION, ScreenType.GAME_LOADING,
                          ScreenType.GAME_CONNECTING, ScreenType.LIVE_GAME_STREAM]), "activate Play"

        if gs.screen_type in (ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY):
            return self._navigation_action(gs, goal, caps, state)
        return None, ""

    @staticmethod
    def _search_first(goal: Goal, state: GraphState) -> bool:
        scenario = state.get("scenario")
        text = " ".join([str(getattr(scenario, "id", "")),
                         str(getattr(scenario, "title", "")),
                         str(getattr(scenario, "intent", "")),
                         str(goal.description)]).lower()
        return "search" in text

    @staticmethod
    def _search_visible(gs: GameState) -> bool:
        blob = " ".join(gs.visible_text + gs.evidence + [gs.focus.element or ""])
        if gs.observation:
            blob += " " + gs.observation.screen_description
        low = blob.lower()
        return any(x in low for x in ("search", "search games", "search for a game",
                                      "type to search", "find games", "game search"))

    def _search_action(self, gs: GameState, goal: Goal,
                       caps: Capabilities | None, state: GraphState,
                       buttons: set[str]):
        if self._search_visible(gs) and goal.target:
            return Action(type=ActionType.OBSERVE, control="__adb_text__",
                          rationale=(f"search panel is visible/focused; type {goal.target!r} "
                                     "using the ADB text fixture"),
                          expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                           ScreenType.GAME_FOCUSED]), "search field ready"
        if "y" in buttons:
            return Action(type=ActionType.PRESS, control="y",
                          rationale="search-first flow: press physical Y to open xCloud Search",
                          expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                           ScreenType.OVERLAY, ScreenType.KEYBOARD]), "open search with Y"
        return self._navigation_action(gs, goal, caps, state)

    def _navigation_action(self, gs: GameState, goal: Goal,
                           caps: Capabilities | None, state: GraphState):
        buttons = {b.lower() for b in (caps.buttons if caps else [])}
        direction = self._choose_direction(buttons)
        if direction is None:
            return None, "no navigation control available"
        return Action(type=ActionType.PRESS, control=direction,
                      rationale=(f"stable {gs.screen_type.value}; establish/move focus "
                                 f"with one {direction} press and observe"),
                      expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                       ScreenType.GAME_FOCUSED, ScreenType.GAME_DETAIL]), "stable xCloud navigation"

    @staticmethod
    def _choose_direction(buttons: set[str]) -> str | None:
        for candidate in ("down", "right", "left", "up"):
            if candidate in buttons:
                return candidate
        return None

    def _force_progress(self, gs: GameState, goal: Goal,
                        caps: Capabilities | None, state: GraphState):
        if self._search_first(goal, state) and gs.screen_type in (
                ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                ScreenType.OVERLAY, ScreenType.KEYBOARD):
            return self._search_action(gs, goal, caps, state,
                                       {b.lower() for b in (caps.buttons if caps else [])})
        if gs.screen_type not in (ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY):
            return None
        if gs.is_waiting_state() or gs.is_fatal():
            return None
        return self._navigation_action(gs, goal, caps, state)

    def _wait_seconds(self, gs: GameState, caps: Capabilities | None) -> float:
        timing = (caps.timing if caps else {}) or {}
        if gs.screen_type is ScreenType.GAME_SPLASH:
            return float(timing.get("game_boot_wait", 8.0))
        if gs.screen_type in (ScreenType.QUEUE, ScreenType.NETWORK_WAIT):
            return float(timing.get("stream_start_wait", 10.0))
        if gs.screen_type in (ScreenType.GAME_LOADING, ScreenType.GAME_CONNECTING):
            return float(timing.get("loading_observation_interval", 3.0))
        return float(timing.get("menu_transition_wait", 2.0))

    def _prompt(self, state: GraphState, gs: GameState, goal: Goal) -> str:
        parts = ["GOAL", f"{goal.description}", f"target={goal.target or 'none'}",
                 "SCREEN", gs.summary()]
        if gs.visible_text:
            parts.append("visible=" + " | ".join(gs.visible_text[:20]))
        if gs.focus.element:
            parts.append(f"focus={gs.focus.element!r}")
        parts.append(self.capability_block(state))
        history = self._history(state)
        if history:
            parts.append("RECENT:\n" + history)
        return "\n".join(parts)

    @staticmethod
    def _history(state: GraphState, limit: int = 6) -> str:
        transitions: list[Transition] = list(state.get("transitions", []))
        return "\n".join(item.describe() for item in transitions[-limit:])

    def _fallback(self, gs: GameState, goal: Goal,
                  caps: Capabilities | None, state: GraphState) -> Action:
        if self._search_first(goal, state) and gs.screen_type in (
                ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                ScreenType.OVERLAY, ScreenType.KEYBOARD):
            action, _ = self._search_action(gs, goal, caps, state,
                                             {b.lower() for b in (caps.buttons if caps else [])})
            if action:
                return action
        nav = self._navigation_action(gs, goal, caps, state)
        return nav[0] if nav else Action(type=ActionType.OBSERVE,
                                         rationale="no safe action available; observe",
                                         expected_states=[])
