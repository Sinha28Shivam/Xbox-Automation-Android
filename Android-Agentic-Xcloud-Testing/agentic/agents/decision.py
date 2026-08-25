"""Closed-loop decision agent for xCloud.

The decision layer chooses ONE action from the currently observed state. Search
is deliberately handled as a guarded state transition: seeing the word
"Search" on the xCloud home page is NOT enough to prove that the search input is
open or focused. Text injection is therefore allowed only after independent
focus evidence is present.
"""

from __future__ import annotations

from ..logbook import log
from ..schemas import Action, ActionType, Capabilities, GameState, Goal, ScreenType, Transition, WAITING_STATES
from ..state import GraphState
from .base import Agent

ROLE = """\
Choose exactly ONE action from the current observed screen.
Use physical gamepad Y to open xCloud Search in search-first scenarios. Never
inject text merely because the page contains the word "Search". Text injection
is allowed only after the search input is independently confirmed as focused or
an explicit keyboard/search-input state is observed. After text is entered,
first dismiss any remaining software keyboard/search-input ownership with B,
then establish physical result focus with D-pad navigation, and only then press
A. Prefer deterministic state evidence over assumptions and never repeat an
action just because the LLM suggested it again.
"""

SEARCH_TEXT_SENTINEL = "__adb_text__"


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
                       caps: Capabilities | None, state: GraphState | None = None):
        buttons = {b.lower() for b in (caps.buttons if caps else [])}
        if goal.is_success(gs):
            return Action(type=ActionType.DONE, rationale=f"goal state {gs.screen_type.value} reached",
                          expected_states=[gs.screen_type]), "goal reached"
        if gs.is_fatal() or goal.is_failure(gs):
            return Action(type=ActionType.DONE, rationale=f"terminal state {gs.screen_type.value}",
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

        if self._search_first(goal, state) and gs.screen_type in (
                ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                ScreenType.OVERLAY, ScreenType.KEYBOARD, ScreenType.GAME_FOCUSED):
            search = self._search_action(gs, goal, caps, state, buttons)
            if search is not None:
                return search

        # A software keyboard/search field can remain active even when the
        # accessibility tree reports the game result as focused. In that state
        # A is NOT a game-selection command. Give the UI back to xCloud first.
        if self._search_input_still_owns_input(gs) and "b" in buttons:
            return Action(type=ActionType.PRESS, control="b",
                          rationale=("software keyboard or search input is still active after text entry; "
                                     "dismiss it before trusting target focus"),
                          expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                           ScreenType.GAME_FOCUSED, ScreenType.KEYBOARD, ScreenType.OVERLAY]), "dismiss search keyboard"

        if (gs.screen_type in (ScreenType.DIALOG, ScreenType.OVERLAY) or gs.overlay_present) and "b" in buttons:
            return Action(type=ActionType.PRESS, control="b",
                          rationale="dismiss visible dialog/overlay",
                          expected_states=[ScreenType.XCLOUD_HOME, ScreenType.GAME_FOCUSED, ScreenType.GAME_DETAIL]), "dismiss overlay"
        if gs.target_focused and "a" in buttons:
            if self._last_a_was_silent_failure(state):
                direction = self._choose_direction(buttons)
                if direction:
                    return Action(type=ActionType.PRESS, control=direction,
                                  rationale=("the previous A produced no screen reaction; "
                                             f"do not repeat A blindly, recover focus with {direction} and observe"),
                                  expected_states=[ScreenType.GAME_FOCUSED, ScreenType.GAME_DETAIL,
                                                   ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY]), "recover after ignored A"
                return Action(type=ActionType.OBSERVE,
                              rationale="the previous A produced no reaction and no navigation control is available; observe before retrying",
                              expected_states=[ScreenType.GAME_FOCUSED, ScreenType.GAME_DETAIL]), "recover after ignored A"
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
    def _search_first(goal: Goal, state: GraphState | None) -> bool:
        if not state:
            return "search" in (goal.description or "").lower()
        scenario = state.get("scenario")
        text = " ".join([str(getattr(scenario, "id", "")), str(getattr(scenario, "title", "")),
                         str(getattr(scenario, "intent", "")), str(goal.description)]).lower()
        return "search" in text

    @staticmethod
    def _search_text_already_sent(state: GraphState | None) -> bool:
        if not state:
            return False
        transitions: list[Transition] = list(state.get("transitions", []))
        return any(t.action and t.action.control == SEARCH_TEXT_SENTINEL for t in transitions[-12:])

    @staticmethod
    def _actions_since_search_text(state: GraphState | None) -> int:
        if not state:
            return 0
        transitions: list[Transition] = list(state.get("transitions", []))
        indices = [i for i, t in enumerate(transitions) if t.action and t.action.control == SEARCH_TEXT_SENTINEL]
        if not indices:
            return 0
        return len(transitions) - 1 - indices[-1]

    @staticmethod
    def _count_recent_action(state: GraphState | None, control: str, limit: int = 8) -> int:
        if not state:
            return 0
        transitions: list[Transition] = list(state.get("transitions", []))
        return sum(1 for t in transitions[-limit:]
                   if t.action and (t.action.control or "").lower() == control.lower())

    @staticmethod
    def _count_action_since(state: GraphState | None, control: str,
                            reset_control: str, limit: int = 20) -> int:
        """Count an action only since the last reset action.

        Search retries must be scoped to the current recovery cycle. Otherwise
        two Y presses from the previous cycle permanently consume the Y budget
        even after B has reset the UI.
        """
        if not state:
            return 0
        transitions: list[Transition] = list(state.get("transitions", []))[-limit:]
        start = 0
        for index in range(len(transitions) - 1, -1, -1):
            action = transitions[index].action
            if action and (action.control or "").lower() == reset_control.lower():
                start = index + 1
                break
        return sum(
            1 for item in transitions[start:]
            if item.action and (item.action.control or "").lower() == control.lower()
        )

    @staticmethod
    def _last_a_was_silent_failure(state: GraphState | None) -> bool:
        """Return True only when the immediately previous action was A and
        neither the glance nor settled observation saw a reaction.

        This prevents the closed loop from spending multiple iterations on the
        same A press when the UI focus assumption was wrong. A transient or
        successful A remains retryable because the transition contains evidence
        that the UI reacted.
        """
        if not state:
            return False
        transition = state.get("last_transition")
        if transition is None:
            transitions: list[Transition] = list(state.get("transitions", []))
            transition = transitions[-1] if transitions else None
        if transition is None or transition.action is None:
            return False
        return ((transition.action.control or "").lower() == "a"
                and bool(transition.silent_failure))

    @classmethod
    def _focus_blob(cls, gs: GameState) -> str:
        parts = [gs.focus.element or ""]
        if gs.observation is not None:
            parts.append(gs.observation.focused_tile or "")
        return " ".join(parts).strip().lower()

    @classmethod
    def _search_input_still_owns_input(cls, gs: GameState) -> bool:
        """Detect the Android IME/search field even when xCloud's result node
        is also reported as focused by the accessibility tree.

        The failed run exposed exactly this race: after ADB text injection the
        result label was present in `focused_tile`, but the on-screen keyboard
        was still visible. Treating `focused_tile == target` as sufficient made
        the agent send A three times into the wrong focus context.
        """
        if gs.screen_type is ScreenType.KEYBOARD:
            return True

        obs = gs.observation
        if obs is None:
            return False

        focus = cls._focus_blob(gs)
        if any(term in focus for term in (
                "search", "search box", "search field", "search input",
                "edittext", "text field", "textbox")):
            return True

        text = " ".join((
            obs.screen_text or "",
            obs.screen_description or "",
            " ".join(obs.notes or []),
        )).lower()
        keyboard_terms = (
            "on-screen keyboard", "onscreen keyboard", "software keyboard",
            "keyboard is visible", "keyboard remains visible", "keyboard overlay",
            "android keyboard", "gboard", "input method", "ime",
            "auto-fill suggestions available above the keyboard",
        )
        if any(term in text for term in keyboard_terms):
            return True

        # ADB text was used recently and the search UI still exposes a text
        # field/cursor. This is intentionally conservative: it only runs in the
        # search-first path and is therefore not a generic B-on-every-screen rule.
        return any(term in text for term in (
            "text cursor", "search field", "search input", "type to search",
            "enter search")) and cls._has_recent_search_text(gs)

    @staticmethod
    def _has_recent_search_text(gs: GameState) -> bool:
        obs = gs.observation
        if obs is None:
            return False
        return "__adb_text__" in " ".join(obs.notes or [])

    @classmethod
    def _search_field_ready(cls, gs: GameState) -> bool:
        if gs.screen_type is ScreenType.KEYBOARD:
            return True
        focus = cls._focus_blob(gs)
        if any(term in focus for term in ("search", "input", "edittext", "text field", "textbox", "search box", "search field")):
            return True
        obs = gs.observation
        if obs is None:
            return False
        text = (obs.screen_text + " " + obs.screen_description).lower()
        has_field = any(term in text for term in (
            "search field", "search box", "search input", "type to search",
            "enter search", "text field", "textbox"))
        has_focus = any(term in text for term in (
            "focused", "focus is", "cursor", "text cursor", "keyboard"))
        return has_field and has_focus

    @classmethod
    def _search_visible(cls, gs: GameState) -> bool:
        return cls._search_field_ready(gs)

    def _search_action(self, gs: GameState, goal: Goal,
                       caps: Capabilities | None, state: GraphState | None,
                       buttons: set[str]):
        if self._search_text_already_sent(state):
            # IMPORTANT: result-node focus from the accessibility tree does not
            # prove that the Android IME has released ownership. Dismiss the
            # keyboard first and only evaluate target_focused on the next look.
            if self._search_input_still_owns_input(gs) and "b" in buttons:
                return Action(type=ActionType.PRESS, control="b",
                              rationale=("search text was entered but the software keyboard/search input "
                                         "still appears active; press B to dismiss it before selecting the result"),
                              expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                               ScreenType.GAME_FOCUSED, ScreenType.KEYBOARD, ScreenType.OVERLAY]), "dismiss keyboard after search text"
            if gs.target_focused and "a" in buttons:
                if self._last_a_was_silent_failure(state):
                    direction = self._choose_direction(buttons)
                    if direction:
                        return Action(type=ActionType.PRESS, control=direction,
                                      rationale=("target was reported focused, but the previous A produced no reaction; "
                                                 f"recover focus with {direction} and re-observe instead of repeating A"),
                                      expected_states=[ScreenType.GAME_FOCUSED, ScreenType.GAME_DETAIL,
                                                       ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY]), "recover search focus after ignored A"
                else:
                    return Action(type=ActionType.PRESS, control="a",
                                  rationale=f"target {goal.target!r} is focused; select it with physical A",
                                  expected_states=[ScreenType.GAME_DETAIL, ScreenType.FULLSCREEN_TRANSITION,
                                                   ScreenType.GAME_LOADING, ScreenType.GAME_CONNECTING,
                                                   ScreenType.LIVE_GAME_STREAM, ScreenType.GAME_SPLASH]), "target focused after search"
            if gs.screen_type is ScreenType.GAME_DETAIL and "a" in buttons:
                return Action(type=ActionType.PRESS, control="a",
                              rationale="game detail page is open; activate Play with physical A",
                              expected_states=[ScreenType.FULLSCREEN_TRANSITION, ScreenType.GAME_LOADING,
                                               ScreenType.GAME_CONNECTING, ScreenType.LIVE_GAME_STREAM]), "activate Play after search"
            nudges = self._actions_since_search_text(state)
            budget = int(self.s.get("execution.closed_loop.search_nav_budget", 6))
            if nudges < budget:
                direction = "down" if "down" in buttons else self._choose_direction(buttons)
                if direction:
                    return Action(type=ActionType.PRESS, control=direction,
                                  rationale=(f"search text {goal.target!r} was typed but result focus is not confirmed; "
                                             f"nudge with {direction} and re-observe"),
                                  expected_states=[ScreenType.GAME_FOCUSED, ScreenType.XCLOUD_HOME,
                                                   ScreenType.XCLOUD_LIBRARY, ScreenType.GAME_DETAIL]), "focus search result"
            return self._navigation_action(gs, goal, caps, state)

        if not self._search_field_ready(gs):
            y_attempts = self._count_action_since(state, "y", "b")
            max_y_attempts = int(self.s.get("execution.closed_loop.search_y_attempts", 2))
            if "y" in buttons and y_attempts < max_y_attempts:
                return Action(type=ActionType.PRESS, control="y",
                              rationale=(f"search field is not confirmed; attempt physical Y "
                                         f"{y_attempts + 1}/{max_y_attempts} in this search cycle and observe"),
                              expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                               ScreenType.OVERLAY, ScreenType.KEYBOARD]), "open search with Y"
            resets = self._count_recent_action(state, "b", 16)
            max_resets = int(self.s.get("execution.closed_loop.search_reset_budget", 2))
            if "b" in buttons and resets < max_resets:
                return Action(type=ActionType.PRESS, control="b",
                              rationale=("two physical Y attempts did not produce independently verified search focus; "
                                         "reset the xCloud shell with B, then observe before another search attempt"),
                              expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY]), "reset failed search transition"
            return Action(type=ActionType.OBSERVE,
                          rationale="search transition budget exhausted; gather fresh evidence before acting",
                          expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                            ScreenType.KEYBOARD, ScreenType.OVERLAY]), "search needs diagnosis"

        if goal.target:
            return Action(type=ActionType.OBSERVE, control=SEARCH_TEXT_SENTINEL,
                          rationale=(f"search input is independently confirmed focused; type {goal.target!r} using the ADB fixture, then observe results"),
                          expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                           ScreenType.GAME_FOCUSED]), "search field ready"
        return self._navigation_action(gs, goal, caps, state)

    def _navigation_action(self, gs: GameState, goal: Goal,
                           caps: Capabilities | None, state: GraphState | None):
        buttons = {b.lower() for b in (caps.buttons if caps else [])}
        direction = self._choose_direction(buttons)
        if direction is None:
            return None, "no navigation control available"
        return Action(type=ActionType.PRESS, control=direction,
                      rationale=(f"stable {gs.screen_type.value}; establish/move focus with one {direction} press and observe"),
                      expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                       ScreenType.GAME_FOCUSED, ScreenType.GAME_DETAIL]), "stable xCloud navigation"

    @staticmethod
    def _choose_direction(buttons: set[str]) -> str | None:
        for candidate in ("down", "right", "left", "up"):
            if candidate in buttons:
                return candidate
        return None

    def _force_progress(self, gs: GameState, goal: Goal,
                        caps: Capabilities | None, state: GraphState | None):
        if self._search_first(goal, state) and gs.screen_type in (
                ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                ScreenType.OVERLAY, ScreenType.KEYBOARD, ScreenType.GAME_FOCUSED):
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
        parts = ["GOAL", f"{goal.description}", f"target={goal.target or 'none'}", "SCREEN", gs.summary()]
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
    def _history(state: GraphState, limit: int = 8) -> str:
        transitions: list[Transition] = list(state.get("transitions", []))
        return "\n".join(item.describe() for item in transitions[-limit:])

    def _fallback(self, gs: GameState, goal: Goal,
                  caps: Capabilities | None, state: GraphState | None) -> Action:
        if self._search_first(goal, state) and gs.screen_type in (
                ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                ScreenType.OVERLAY, ScreenType.KEYBOARD, ScreenType.GAME_FOCUSED):
            action, _ = self._search_action(gs, goal, caps, state,
                                             {b.lower() for b in (caps.buttons if caps else [])})
            if action:
                return action
        nav = self._navigation_action(gs, goal, caps, state)
        return nav[0] if nav else Action(type=ActionType.OBSERVE,
                                         rationale="no safe action available; observe",
                                         expected_states=[])
