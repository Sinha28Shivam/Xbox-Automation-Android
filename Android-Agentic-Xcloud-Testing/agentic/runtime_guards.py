"""Small runtime guards for closed-loop launch recovery.

The main agents stay readable and generic. These guards add the evidence-driven
rules that are easiest to regress: recognize Android's lock/home screen, bound a
stuck fullscreen handoff, and re-announce the controller after a browser relaunch.
They are installed once from ``agentic.agents`` after the normal agent classes
have been imported.
"""

from __future__ import annotations

from .schemas import Action, ActionType, ScreenType, Transition


def _is_android_screen(gs) -> bool:
    if gs is None or gs.screen_type is ScreenType.ANDROID_HOME:
        return True if gs is not None else False
    obs = gs.observation
    if obs is None:
        return False
    text = (obs.screen_text + " " + obs.screen_description + " "
            + (obs.focused_window or "")).lower()
    return any(marker in text for marker in (
        "android system",
        "wireless debugging connected",
        "tap to turn off wireless debugging",
        "bluetooth connected",
        "4g phone",
        "wi-fi signal full",
    ))


def _recent_consecutive_state(transitions: list[Transition], state: ScreenType) -> int:
    count = 0
    for transition in reversed(transitions):
        if transition.state_after is not state:
            break
        count += 1
    return count


def install_runtime_guards() -> None:
    """Install deterministic launch guards once per Python process."""
    from .agents.decision import DecisionAgent
    from .agents.observer import ObserverAgent
    from .perception.state_builder import StateBuilder

    if getattr(DecisionAgent, "_launch_guards_installed", False):
        return

    # ------------------------------------------------------------------
    # Perception guard: the Android lock screen must never be interpreted as
    # xCloud's press-any-button state merely because the phone contains the word
    # "button" or the vision model is uncertain.
    original_fast = StateBuilder._fast

    def guarded_fast(self, obs, goal=None, previous=None):
        state = original_fast(self, obs, goal, previous)
        text = (obs.screen_text + " " + obs.screen_description + " "
                + (obs.focused_window or "")).lower()
        lock_cues = (
            "android system" in text
            and "wireless debugging connected" in text
        ) or "tap to turn off wireless debugging" in text
        if lock_cues:
            state.screen_type = ScreenType.ANDROID_HOME
            state.application = "android"
            state.loading = False
            state.game_running = False
            state.target_visible = False
            state.target_focused = False
            state.confidence = 0.98
            state.source = "fast"
            state.evidence = list(state.evidence) + [
                "Android system/lock-screen UI is explicitly visible; this is "
                "an unexpected Android screen, not xCloud press-any-button"
            ]
        return state

    StateBuilder._fast = guarded_fast

    # ------------------------------------------------------------------
    # Decision guard: waiting is valid only while it is making progress. After
    # repeated identical fullscreen observations, announce the controller again
    # instead of spending the remaining iteration budget doing the same wait.
    original_decide = DecisionAgent._deterministic

    def guarded_decide(self, gs, goal, caps, state=None):
        buttons = {b.lower() for b in (caps.buttons if caps else [])}
        transitions = list(state.get("transitions", [])) if state else []
        last_action = transitions[-1].action if transitions else None

        # A relaunch produces a fresh browser page. The first safe action after
        # the page is visible is the existing signal handshake, not navigation.
        if (gs.screen_type in (ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY)
                and last_action is not None
                and last_action.type is ActionType.LAUNCH_PWA
                and "signal_handshake" in (caps.special_actions if caps else {})):
            return Action(
                type=ActionType.MACRO,
                control="signal_handshake",
                rationale="xCloud was relaunched; re-announce the gamepad before sending navigation input",
                expected_states=[ScreenType.XCLOUD_HOME, ScreenType.GAME_FOCUSED,
                                 ScreenType.OVERLAY],
            ), "handshake after browser relaunch"

        # If Android's lock/home screen stole focus, relaunch the configured PWA.
        if _is_android_screen(gs):
            url = str(self.s.get("android.pwa.url", "https://www.xbox.com/play"))
            return Action(
                type=ActionType.LAUNCH_PWA,
                control=url,
                rationale="the observer explicitly detected Android system UI instead of xCloud; restore the xCloud PWA",
                expected_states=[ScreenType.XCLOUD_HOME, ScreenType.XCLOUD_LIBRARY,
                                 ScreenType.GAME_FOCUSED],
            ), "recover unexpected Android screen"

        # Bound a black/fullscreen handoff. The existing verifier still treats
        # each individual fullscreen state as legitimate intermediate progress;
        # this guard adds the missing aggregate-progress rule.
        if gs.screen_type is ScreenType.FULLSCREEN_TRANSITION:
            limit = int(self.s.get(
                "execution.closed_loop.fullscreen_transition.max_repeats", 5))
            repeats = _recent_consecutive_state(transitions, ScreenType.FULLSCREEN_TRANSITION)
            if repeats >= limit and "signal_handshake" in (caps.special_actions if caps else {}):
                return Action(
                    type=ActionType.MACRO,
                    control="signal_handshake",
                    rationale=(f"fullscreen transition produced {repeats} consecutive identical observations; "
                               "re-announce the controller once instead of waiting indefinitely"),
                    expected_states=[ScreenType.XCLOUD_HOME, ScreenType.OVERLAY,
                                     ScreenType.LIVE_GAME_STREAM, ScreenType.GAME_LOADING],
                ), "fullscreen transition watchdog"

        return original_decide(self, gs, goal, caps, state)

    DecisionAgent._deterministic = guarded_decide
    DecisionAgent._launch_guards_installed = True
