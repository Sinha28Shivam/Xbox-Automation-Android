"""decision.py - choose ONE action from the current observed state.

The decision layer is deliberately split into deterministic safety/progress
rules and an LLM fallback. The LLM may choose a direction when the UI is
ambiguous, but it must not trap the run in an OBSERVE-only loop when xCloud is
already identified and stable.
"""

from __future__ import annotations

from ..logbook import log
from ..schemas import (
    Action,
    ActionType,
    Capabilities,
    GameState,
    Goal,
    ScreenType,
    Transition,
    WAITING_STATES,
)
from ..state import GraphState
from .base import Agent


ROLE = """\
You choose exactly ONE gamepad action from the CURRENT screen state.

Hard rules:
1. ONE action only. Never return a sequence or repeat count.
2. `control` must exactly match the capability list.
3. After every directional input the system will observe again.
4. Loading, connecting, fullscreen transition, queue and splash states require WAIT.
5. UNKNOWN or genuinely low-confidence screens require OBSERVE.
6. If xCloud is identified and stable, do NOT repeatedly choose OBSERVE.
   A directional input is required to establish or move focus.
7. If the target is visible but focus is unknown, choose one safe direction
   that moves into or across the target grid; do not ask to observe again.
8. If the target is focused, press A.
9. If the goal state is reached, return DONE.
10. expected_states must contain every legitimate next state for the action.
11. rationale must cite visible state evidence, not generic words such as
    "navigating".
"""


class DecisionAgent(Agent):
    name = "decision"

    def run(self, state: GraphState) -> GraphState:
        gs: GameState | None = state.get("game_state")
        goal: Goal | None = state.get("goal")
        caps: Capabilities | None = state.get("capabilities")

        if gs is None or goal is None:
            return {
                "halt_reason": "the decision agent ran before a state or goal existed",
                "agent_trace": [self.trace("decide", "missing inputs")],
            }

        action, why = self._deterministic(gs, goal, caps, state)
        if action is not None:
            log.act(f"decided (no LLM): {action.describe()} - {why}", indent=1)
            return {
                "pending_action": action,
                "agent_trace": [
                    self.trace("decide", f"{action.describe()} [deterministic] - {why}")
                ],
            }

        decided: Action | None = self.think(
            Action,
            self.system_prompt(ROLE),
            self._prompt(state, gs, goal),
            default=None,
        )

        # A model can still return OBSERVE even though the screen is a known,
        # stable xCloud page. Never allow that to create an infinite observation
        # loop. Convert it into one safe navigation input.
        if decided is not None and decided.type is ActionType.OBSERVE:
            forced = self._force_progress(gs, goal, caps, state)
            if forced is not None:
                decided, why = forced
                log.act(
                    f"overrode LLM observe: {decided.describe()} - {why}",
                    indent=1,
                )

        if decided is None:
            decided = self._fallback(gs, goal, caps)
            log.act(
                f"decided (mechanical fallback): {decided.describe()} - {decided.rationale}",
                indent=1,
            )
        else:
            log.act(
                f"decided: {decided.describe()} - {decided.rationale}",
                indent=1,
            )

        return {
            "pending_action": decided,
            "agent_trace": [
                self.trace("decide", f"{decided.describe()} - {decided.rationale[:160]}")
            ],
        }

    def _deterministic(
        self,
        gs: GameState,
        goal: Goal,
        caps: Capabilities | None,
        state: GraphState,
    ) -> tuple[Action | None, str]:
        """Rules that are safer and more reliable than an LLM decision."""
        buttons = {b.lower() for b in (caps.buttons if caps else [])}

        if goal.is_success(gs):
            return (
                Action(
                    type=ActionType.DONE,
                    rationale=f"the goal state {gs.screen_type.value} has been reached",
                    expected_states=[gs.screen_type],
                ),
                "goal state reached",
            )

        if gs.is_fatal() or goal.is_failure(gs):
            return (
                Action(
                    type=ActionType.DONE,
                    rationale=(
                        f"{gs.screen_type.value} is a terminal failure state; "
                        "no controller action can recover it"
                    ),
                    expected_states=[gs.screen_type],
                ),
                "terminal failure state",
            )

        threshold = float(
            self.s.get("execution.closed_loop.confidence.reobserve", 0.60)
        )
        if gs.screen_type is ScreenType.UNKNOWN or gs.confidence < threshold:
            # A known xCloud screen is handled below even when focus is unknown.
            # Only truly unknown/weak screens enter the observe path.
            return (
                Action(
                    type=ActionType.OBSERVE,
                    rationale=(
                        f"the screen is {gs.screen_type.value} at only "
                        f"{gs.confidence:.0%} confidence, below the "
                        f"{threshold:.0%} action threshold"
                    ),
                    expected_states=[],
                ),
                f"confidence {gs.confidence:.0%} below {threshold:.0%}",
            )

        # A controller prompt has priority over normal navigation and waiting.
        if gs.screen_type is ScreenType.CONTROLLER_PROMPT or gs.controller_prompt:
            if "signal_handshake" in (caps.special_actions if caps else {}):
                return (
                    Action(
                        type=ActionType.MACRO,
                        control="signal_handshake",
                        rationale=(
                            "the page is asking for a controller, so re-run the "
                            "signal handshake before sending normal navigation input"
                        ),
                        expected_states=[
                            ScreenType.XCLOUD_HOME,
                            ScreenType.GAME_FOCUSED,
                            ScreenType.LIVE_GAME_STREAM,
                            ScreenType.IN_GAME,
                        ],
                    ),
                    "controller prompt - re-handshake",
                )
            if "guide" in buttons:
                return (
                    Action(
                        type=ActionType.PRESS,
                        control="guide",
                        rationale=(
                            "the page is asking for a controller and Guide is "
                            "available to announce the pad"
                        ),
                        expected_states=[
                            ScreenType.XCLOUD_HOME,
                            ScreenType.OVERLAY,
                            ScreenType.GAME_FOCUSED,
                        ],
                    ),
                    "controller prompt - announce with Guide",
                )

        if gs.screen_type in WAITING_STATES or gs.loading:
            seconds = self._wait_seconds(gs, caps)
            return (
                Action(
                    type=ActionType.WAIT,
                    seconds=seconds,
                    rationale=(
                        f"{gs.screen_type.value} is transient; wait {seconds:.1f}s "
                        "instead of sending input during the handoff"
                    ),
                    expected_states=list(WAITING_STATES)
                    + [
                        ScreenType.LIVE_GAME_STREAM,
                        ScreenType.PRESS_ANY_BUTTON,
                        ScreenType.GAME_MAIN_MENU,
                    ],
                ),
                f"{gs.screen_type.value} needs time, not input",
            )

        if gs.screen_type is ScreenType.PRESS_ANY_BUTTON and "a" in buttons:
            return (
                Action(
                    type=ActionType.PRESS,
                    control="a",
                    rationale="the screen explicitly asks for a button to continue",
                    expected_states=[
                        ScreenType.GAME_MAIN_MENU,
                        ScreenType.GAME_SPLASH,
                        ScreenType.IN_GAME,
                    ],
                ),
                "press-any-button prompt",
            )

        if (
            gs.screen_type in (ScreenType.DIALOG, ScreenType.OVERLAY)
            or gs.overlay_present
        ) and "b" in buttons:
            return (
                Action(
                    type=ActionType.PRESS,
                    control="b",
                    rationale="a dialog/overlay is present and should be dismissed first",
                    expected_states=[
                        ScreenType.XCLOUD_HOME,
                        ScreenType.GAME_FOCUSED,
                        ScreenType.GAME_DETAIL,
                        ScreenType.LIVE_GAME_STREAM,
                    ],
                ),
                "dismiss overlay",
            )

        if gs.target_focused and "a" in buttons:
            return (
                Action(
                    type=ActionType.PRESS,
                    control="a",
                    rationale=f"the target {goal.target!r} is focused, so select it",
                    expected_states=[
                        ScreenType.GAME_DETAIL,
                        ScreenType.FULLSCREEN_TRANSITION,
                        ScreenType.GAME_LOADING,
                        ScreenType.GAME_CONNECTING,
                        ScreenType.LIVE_GAME_STREAM,
                        ScreenType.GAME_SPLASH,
                    ],
                ),
                "target focused - select it",
            )

        if gs.screen_type is ScreenType.GAME_DETAIL and "a" in buttons:
            return (
                Action(
                    type=ActionType.PRESS,
                    control="a",
                    rationale="the game detail page is open; activate its default Play action",
                    expected_states=[
                        ScreenType.FULLSCREEN_TRANSITION,
                        ScreenType.GAME_LOADING,
                        ScreenType.GAME_CONNECTING,
                        ScreenType.LIVE_GAME_STREAM,
                    ],
                ),
                "detail page - activate Play",
            )

        # Critical fix: a stable xCloud screen with a visible target must make
        # progress even when the focus sensor cannot identify the highlight.
        # This is the exact state that previously produced endless OBSERVE calls.
        if gs.screen_type in (ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY):
            forced = self._navigation_action(gs, goal, caps, state)
            if forced is not None:
                return forced

        return None, ""

    def _navigation_action(
        self,
        gs: GameState,
        goal: Goal,
        caps: Capabilities | None,
        state: GraphState,
    ) -> tuple[Action, str] | None:
        """Return one safe navigation press for a stable xCloud page."""
        buttons = {b.lower() for b in (caps.buttons if caps else [])}
        if not buttons:
            return None

        direction = self._choose_direction(gs, goal, buttons, state)
        if direction is None:
            return None

        if gs.target_visible and not gs.target_focused:
            reason = (
                f"{goal.target!r} is visible but the highlight is not reliably "
                f"identified; send one {direction} press to establish/move focus "
                "and observe the resulting screen"
            )
        else:
            reason = (
                f"{gs.screen_type.value} is stable and no actionable focus is "
                f"known; send one {direction} press to establish focus"
            )

        return (
            Action(
                type=ActionType.PRESS,
                control=direction,
                rationale=reason,
                expected_states=[
                    ScreenType.XCLOUD_HOME,
                    ScreenType.XCLOUD_LIBRARY,
                    ScreenType.GAME_FOCUSED,
                    ScreenType.GAME_DETAIL,
                ],
            ),
            "stable xCloud page - deterministic navigation",
        )

    @staticmethod
    def _choose_direction(
        gs: GameState,
        goal: Goal,
        buttons: set[str],
        state: GraphState,
    ) -> str | None:
        """Choose a conservative first direction without game-specific scripts.

        `down` is preferred on the xCloud home/library screen because it moves
        from the shell/rail toward the content grid. Once focus exists, the LLM
        gets the next directional decision. We never issue more than one press.
        """
        if gs.screen_type in (ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY):
            if "down" in buttons:
                return "down"
            if "right" in buttons:
                return "right"
            if "left" in buttons:
                return "left"
            if "up" in buttons:
                return "up"

        # If the state exposes a known previous navigation action, avoid simply
        # repeating it when the screen did not change.
        transitions: list[Transition] = list(state.get("transitions", []))
        if transitions:
            last = transitions[-1].action
            previous_direction = last.control if last else None
            for candidate in ("right", "down", "left", "up"):
                if candidate in buttons and candidate != previous_direction:
                    return candidate

        for candidate in ("right", "down", "left", "up"):
            if candidate in buttons:
                return candidate
        return None

    def _force_progress(
        self,
        gs: GameState,
        goal: Goal,
        caps: Capabilities | None,
        state: GraphState,
    ) -> tuple[Action, str] | None:
        """Reject an unnecessary LLM OBSERVE on a known stable xCloud page."""
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
        parts = [
            "THE GOAL",
            f"  {goal.description or 'reach the target state'}",
            f"  target: {goal.target or 'not specified'}",
            "  success states: "
            + (", ".join(s.value for s in goal.success_states) or "not specified"),
            "",
            "THE SCREEN RIGHT NOW",
            f"  {gs.summary()}",
        ]
        if gs.focus.element:
            parts.append(f"  the highlight is on: {gs.focus.element!r}")
        else:
            parts.append(
                "  focus is unknown; if the screen is a stable xCloud page, "
                "choose one direction instead of OBSERVE"
            )
        if gs.visible_text:
            parts.append("  text visible on screen:")
            parts += [f"    {line}" for line in gs.visible_text[:25]]
        if gs.evidence:
            parts.append("  evidence:")
            parts += [f"    - {e}" for e in gs.evidence[:6]]

        parts += ["", self.capability_block(state)]
        history = self._history(state)
        if history:
            parts += [
                "",
                "WHAT HAS ALREADY BEEN TRIED (most recent last)",
                history,
                "",
                "Do not repeat an action that produced no change twice in a row.",
            ]
        return "\n".join(parts)

    @staticmethod
    def _history(state: GraphState, limit: int = 6) -> str:
        transitions: list[Transition] = list(state.get("transitions", []))
        if not transitions:
            return ""
        lines: list[str] = []
        for item in transitions[-limit:]:
            lines.append(
                f"  {item.describe()}"
                + (
                    f" ({item.failure_class.value})"
                    if item.failure_class.value != "none"
                    else ""
                )
            )
        return "\n".join(lines)

    def _fallback(
        self,
        gs: GameState,
        goal: Goal,
        caps: Capabilities | None,
    ) -> Action:
        buttons = {b.lower() for b in (caps.buttons if caps else [])}

        if gs.screen_type in (ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY):
            direction = self._choose_direction(gs, goal, buttons, {})
            if direction:
                return Action(
                    type=ActionType.PRESS,
                    control=direction,
                    rationale=(
                        "no LLM available; the xCloud page is stable, so send one "
                        f"{direction} press and observe the result"
                    ),
                    expected_states=[
                        ScreenType.XCLOUD_HOME,
                        ScreenType.XCLOUD_LIBRARY,
                        ScreenType.GAME_FOCUSED,
                        ScreenType.GAME_DETAIL,
                    ],
                )

        return Action(
            type=ActionType.OBSERVE,
            rationale=(
                "no LLM available and no safe mechanical rule fits this screen; "
                "observe rather than send an arbitrary controller command"
            ),
            expected_states=[],
        )
