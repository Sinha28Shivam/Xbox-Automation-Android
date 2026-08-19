"""
state_builder.py - Observation -> GameState. The eyes of the closed loop.

WHAT THIS REPLACES, AND WHY IT HAD TO CHANGE
--------------------------------------------
Before this module, "state" was a handful of independent booleans set by
substring tests at the end of `vision.observe()`:

    combined = f"{ocr_text}\\n{llm_description}".lower()
    if "minecraft" in combined or "dungeons" in combined:
        obs.target_visible = True

Three faults, all of which this module exists to remove:

1. It was a false-positive generator. `target_visible` became true if the word
   appeared ANYWHERE in the combined text - including inside the vision model's
   own sentence "there is no Minecraft tile visible on this screen".

2. The booleans were mutually independent, so `detail_page_open`,
   `stream_active` and `main_menu_visible` could all be true at once. With no
   single value for "what screen is this", `state_before -> action ->
   state_after` could not be expressed, and so transitions could not be
   verified at all.

3. It was game-specific in the perception layer. Adding a second game meant
   editing the code that reads pixels, which is exactly backwards.

TWO TIERS, CHEAPEST FIRST
-------------------------
    FAST     frame diff + OCR + geometry. No API call, ~milliseconds.
    ESCALATE one vision-LLM classification, ONLY when the fast tier is unsure.

This is where the wall-clock win lives. The previous pipeline called a vision
LLM on the glance AND the settle of every acting step - two calls per step
before anything asked whether they were needed. A confident navigation step now
costs zero.

The floor is honesty, as everywhere else in this project: when the sensors are
absent or the cues conflict, `screen_type` stays UNKNOWN with a low confidence
and `evidence` records why. UNKNOWN is a real answer that the decision agent
handles by looking again - it is never quietly rounded to something actionable.

NOTHING HERE NAMES A GAME
-------------------------
Target matching uses `goal.target`, the string the scenario supplied. Cues are
generic xCloud/console vocabulary ("starting your game", "press any button").
A new title is a new YAML file, not an edit here.
"""

from __future__ import annotations

import re
from typing import Any

from ..logbook import log
from ..schemas import (Focus, GameState, Goal, Observation, ScreenType)
from ..settings import Settings

# --------------------------------------------------------------------------
# Generic cue tables.
#
# Phrases only - never a game title. Each entry is (screen_type, weight): the
# weight is how much seeing this phrase should move our belief, because "an
# error occurred" is far more diagnostic than the word "play" appearing
# somewhere on a busy store page.
#
# Matching is done on WORD BOUNDARIES against OCR text. Substring matching is
# what made the old `"play" in combined` test fire on the word "Player", and on
# "Replay", and on a game called "Playdead".
# --------------------------------------------------------------------------
_CUES: tuple[tuple[str, ScreenType, float], ...] = (
    # Loading / connecting. These are the states whose absence caused a
    # successful launch to be reported as a failure.
    ("starting your game", ScreenType.GAME_LOADING, 0.9),
    ("we are starting your game", ScreenType.GAME_LOADING, 0.9),
    ("getting your game ready", ScreenType.GAME_LOADING, 0.9),
    ("connecting", ScreenType.GAME_CONNECTING, 0.6),
    ("loading", ScreenType.GAME_LOADING, 0.5),
    ("please wait", ScreenType.GAME_LOADING, 0.5),

    # Queue / network
    ("you are in line", ScreenType.QUEUE, 0.9),
    ("in the queue", ScreenType.QUEUE, 0.85),
    ("waiting in queue", ScreenType.QUEUE, 0.85),
    ("estimated wait", ScreenType.QUEUE, 0.7),
    ("check your connection", ScreenType.NETWORK_WAIT, 0.7),
    ("slow connection", ScreenType.NETWORK_WAIT, 0.6),

    # Errors - deliberately high weight: acting on an error screen wastes the
    # rest of the run, so a single clear cue should be enough to stop.
    ("something went wrong", ScreenType.STREAM_ERROR, 0.9),
    ("an error occurred", ScreenType.STREAM_ERROR, 0.9),
    ("unable to start", ScreenType.STREAM_ERROR, 0.85),
    ("stream error", ScreenType.STREAM_ERROR, 0.9),
    ("disconnected", ScreenType.STREAM_ERROR, 0.7),
    ("try again", ScreenType.STREAM_ERROR, 0.4),

    # Auth
    ("sign in", ScreenType.LOGIN, 0.8),
    ("sign-in", ScreenType.LOGIN, 0.8),
    ("session expired", ScreenType.SESSION_EXPIRED, 0.9),
    ("session has expired", ScreenType.SESSION_EXPIRED, 0.9),
    ("please sign in again", ScreenType.SESSION_EXPIRED, 0.85),

    # Game boot
    ("press any button", ScreenType.PRESS_ANY_BUTTON, 0.9),
    ("press a to continue", ScreenType.PRESS_ANY_BUTTON, 0.9),
    ("press start", ScreenType.PRESS_ANY_BUTTON, 0.85),

    # Controller prompt - it steals input, so naming it matters
    ("connect a controller", ScreenType.CONTROLLER_PROMPT, 0.85),
    ("controller required", ScreenType.CONTROLLER_PROMPT, 0.8),
    ("press a button on your controller", ScreenType.CONTROLLER_PROMPT, 0.85),

    # Detail page
    ("play now", ScreenType.GAME_DETAIL, 0.6),
    ("add to my list", ScreenType.GAME_DETAIL, 0.5),
    ("similar games", ScreenType.GAME_DETAIL, 0.5),

    # xCloud shell
    ("jump back in", ScreenType.XCLOUD_HOME, 0.7),
    ("recently played", ScreenType.XCLOUD_HOME, 0.6),
    ("game pass", ScreenType.XCLOUD_HOME, 0.4),
    ("most popular", ScreenType.XCLOUD_HOME, 0.5),
    ("cloud gaming", ScreenType.XCLOUD_HOME, 0.4),
    ("my library", ScreenType.XCLOUD_LIBRARY, 0.7),
    ("full library", ScreenType.XCLOUD_LIBRARY, 0.6),

    # In-game menus. Generic console vocabulary, not one game's wording.
    ("main menu", ScreenType.GAME_MAIN_MENU, 0.7),
    ("new game", ScreenType.GAME_MAIN_MENU, 0.6),
    ("continue", ScreenType.GAME_MAIN_MENU, 0.3),
    ("load game", ScreenType.GAME_MAIN_MENU, 0.6),
    ("campaign", ScreenType.GAME_MAIN_MENU, 0.5),
    ("multiplayer", ScreenType.GAME_MAIN_MENU, 0.5),
    ("marketplace", ScreenType.GAME_MAIN_MENU, 0.4),
    ("resume", ScreenType.GAME_PAUSE_MENU, 0.5),
    ("quit to", ScreenType.GAME_PAUSE_MENU, 0.7),

    # Overlays that eat gamepad input
    ("allow", ScreenType.DIALOG, 0.4),
    ("deny", ScreenType.DIALOG, 0.4),
    ("open with", ScreenType.DIALOG, 0.7),
    ("just once", ScreenType.DIALOG, 0.6),
)

# Browser chrome. Its presence means we are in the PWA shell rather than in a
# fullscreen stream. NOT a defect - xCloud is a web page - so this only ever
# informs the classification, never a failure.
_BROWSER_CUES = ("http", "https", "search or type", "new tab", "bookmark",
                 "address bar", "xbox.com")


def _words(text: str) -> str:
    """Normalise OCR output for word-boundary matching.

    OCR is noisy: it emits stray punctuation, doubled spaces and line breaks
    mid-phrase. Collapsing all non-alphanumerics to single spaces lets
    "Starting  your\\ngame!" match the cue "starting your game", while keeping
    boundaries so "play" cannot match inside "Player".
    """
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "


class StateBuilder:
    """Builds a `GameState` from an `Observation`. One instance per run."""

    def __init__(self, settings: Settings, vision: Any, llm: Any = None):
        self.s = settings
        self.vision = vision
        self.llm = llm
        # Counters the report uses to show the fast tier is earning its keep.
        self.fast_resolved = 0
        self.escalated = 0

    # -- thresholds --------------------------------------------------------
    @property
    def act_threshold(self) -> float:
        return float(self.s.get("execution.closed_loop.confidence.act", 0.85))

    @property
    def reobserve_threshold(self) -> float:
        return float(
            self.s.get("execution.closed_loop.confidence.reobserve", 0.60))

    # -- entry point -------------------------------------------------------
    def build(self, observation: Observation, goal: Goal | None = None,
              previous: GameState | None = None) -> GameState:
        """Interpret one observation. Never raises; degrades to UNKNOWN."""
        state = self._fast(observation, goal, previous)

        escalate = (
            state.confidence < self.reobserve_threshold
            and bool(self.s.get(
                "execution.closed_loop.perception.vision_on_low_confidence",
                True))
            and observation.screenshot_path is not None
        )

        if escalate:
            refined = self._escalate(observation, goal, state)
            if refined is not None:
                self.escalated += 1
                log.see(f"state escalated to the vision LLM "
                        f"(fast tier was only {state.confidence:.0%} sure): "
                        f"{refined.summary()}", indent=2)
                return refined
        else:
            self.fast_resolved += 1

        return state

    # ------------------------------------------------------------------
    # TIER 1 - fast, free, mechanical
    # ------------------------------------------------------------------
    def _fast(self, obs: Observation, goal: Goal | None,
              previous: GameState | None) -> GameState:
        state = GameState(observation=obs, source="fast")
        evidence: list[str] = []

        text = obs.screen_text or ""
        # The vision LLM's prose is used as a WEAK extra text source when it
        # happens to be present (a legacy plan-mode step may have filled it).
        # It is never the only basis for a claim, because a description is the
        # model's opinion, not a reading.
        haystack = _words(text + "\n" + (obs.screen_description or ""))

        if not obs.sensors_used:
            state.confidence = 0.0
            state.evidence = ["no sensor produced any data, so no claim about "
                              "the screen can be made"]
            return state

        # -- visible text ------------------------------------------------
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        state.visible_text = lines[:40]

        # -- score every cue --------------------------------------------
        scores: dict[ScreenType, float] = {}
        for phrase, screen, weight in _CUES:
            if f" {phrase} " in haystack:
                # Take the strongest cue per state rather than summing: three
                # weak hints should not out-vote one decisive phrase, and
                # summing lets a busy store page accumulate its way to a
                # confident wrong answer.
                scores[screen] = max(scores.get(screen, 0.0), weight)
                evidence.append(f"OCR contains {phrase!r} -> {screen.value}")

        # -- flags independent of screen_type ---------------------------
        state.loading = any(
            s in scores for s in (ScreenType.GAME_LOADING,
                                  ScreenType.GAME_CONNECTING))
        state.error_present = any(
            s in scores for s in (ScreenType.STREAM_ERROR,
                                  ScreenType.SESSION_EXPIRED))
        state.controller_prompt = ScreenType.CONTROLLER_PROMPT in scores
        state.overlay_present = (ScreenType.DIALOG in scores
                                 or ScreenType.KEYBOARD in scores)

        browser_visible = any(cue in haystack for cue in _BROWSER_CUES)
        if browser_visible:
            evidence.append("browser chrome/URL text is visible, so this is "
                            "the PWA shell rather than a fullscreen stream "
                            "(normal for xCloud, not a defect)")

        # -- the target, from the GOAL and never hardcoded --------------
        if goal and goal.target:
            state.target_visible, state.target_focused, focus_note = \
                self._match_target(goal.target, text, obs)
            if focus_note:
                evidence.append(focus_note)
            if state.target_focused:
                scores[ScreenType.GAME_FOCUSED] = max(
                    scores.get(ScreenType.GAME_FOCUSED, 0.0), 0.75)

        # -- focus element ----------------------------------------------
        if obs.focused_tile:
            state.focus = Focus(element=obs.focused_tile, confidence=0.7)
            evidence.append(f"focused tile reported as {obs.focused_tile!r}")

        # -- geometry: a near-black, textless frame is a transition -----
        #
        # This is the cue that no amount of OCR can supply, and the one that was
        # missing when a fullscreen handoff got classified as "not the detail
        # page I expected" and sent to RCA.
        if not lines and obs.screenshot_path:
            darkness = self._darkness(obs.screenshot_path)
            if darkness is not None and darkness > 0.90:
                scores[ScreenType.FULLSCREEN_TRANSITION] = max(
                    scores.get(ScreenType.FULLSCREEN_TRANSITION, 0.0), 0.7)
                evidence.append(
                    f"{darkness:.0%} of the frame is near-black and OCR found "
                    f"no text: characteristic of the xCloud fullscreen handoff")

        # -- app identity ------------------------------------------------
        if obs.focused_window:
            state.application = ("xcloud" if browser_visible
                                 or "chrome" in obs.focused_window.lower()
                                 or "webapk" in obs.focused_window.lower()
                                 else obs.focused_window)

        # -- settle on ONE screen_type ----------------------------------
        if scores:
            best = max(scores.items(), key=lambda kv: kv[1])
            state.screen_type, confidence = best

            # A second, near-equal candidate means the cues genuinely conflict.
            # Reporting the winner at full confidence would be the difference
            # between "I know" and "I guessed", so the confidence is cut and
            # the ambiguity is written down.
            rivals = sorted((v for k, v in scores.items()
                             if k is not state.screen_type), reverse=True)
            if rivals and rivals[0] >= confidence - 0.15:
                confidence *= 0.7
                evidence.append(
                    "cues for more than one screen type are present and close "
                    "in strength, so this classification is not confident")
            state.confidence = round(min(confidence, 0.95), 3)
        else:
            state.screen_type = ScreenType.UNKNOWN
            state.confidence = 0.1 if lines else 0.05
            evidence.append(
                "no known cue matched, so the screen type is unknown - the "
                "correct response is to look again, not to press something")

        state.game_running = state.screen_type in (
            ScreenType.LIVE_GAME_STREAM, ScreenType.GAME_MAIN_MENU,
            ScreenType.GAME_PAUSE_MENU, ScreenType.IN_GAME,
            ScreenType.GAME_SPLASH, ScreenType.PRESS_ANY_BUTTON)

        # An OCR-less run cannot support a confident text-based claim, whatever
        # the cue table said. Better to escalate to the vision tier than to
        # trust a description we did not read.
        if not obs.screen_text and state.confidence > self.reobserve_threshold:
            state.confidence = round(self.reobserve_threshold - 0.01, 3)
            evidence.append(
                "OCR produced no text, so this classification rests on weaker "
                "evidence than usual and is capped below the act threshold")

        state.evidence = evidence
        return state

    # -- target matching ---------------------------------------------------
    @staticmethod
    def _match_target(target: str, text: str,
                      obs: Observation) -> tuple[bool, bool, str]:
        """Is the goal's target visible, and is it focused?

        Deliberately conservative about FOCUS. Visibility can be read from text;
        focus cannot, in general, be read from text at all - a tile's label
        appears identically whether or not it carries the highlight. So focus is
        only claimed when a sensor actually reported it (`focused_tile`).
        Guessing here is what produced "target_focused" on a screen where the
        target was merely one of twenty visible tiles, and then pressing A on
        whatever really had focus.
        """
        needle = re.sub(r"[^a-z0-9]+", " ", target.lower()).strip()
        hay = _words(text)
        visible = bool(needle) and f" {needle} " in hay

        if not visible and needle:
            # Fall back to the distinctive words of a multi-word title, so
            # "Minecraft Dungeons" is still found when OCR drops one word.
            parts = [p for p in needle.split() if len(p) > 3]
            if parts and all(f" {p} " in hay for p in parts):
                visible = True

        focused = False
        note = ""
        if obs.focused_tile and needle:
            tile = re.sub(r"[^a-z0-9]+", " ", obs.focused_tile.lower()).strip()
            focused = needle in tile or tile in needle
            note = (f"the focused element is {obs.focused_tile!r}, which "
                    f"{'matches' if focused else 'does NOT match'} the target "
                    f"{target!r}")
        elif visible:
            note = (f"{target!r} is visible in the on-screen text, but no "
                    f"sensor reported which element holds the highlight, so "
                    f"focus is NOT claimed")
        return visible, focused, note

    # -- geometry ----------------------------------------------------------
    def _darkness(self, path: str) -> float | None:
        """Fraction of the frame that is near-black. None if unmeasurable.

        Reuses whatever imaging libraries `VisionTool` already probed rather
        than importing its own, so a rig without Pillow degrades here exactly
        as it does everywhere else instead of raising.
        """
        try:
            from PIL import Image
            import numpy as np
        except ImportError:
            return None
        try:
            with Image.open(path) as img:
                small = img.convert("L").resize((64, 64))
                arr = np.asarray(small, dtype="int16")
            return float((arr < 24).sum()) / float(arr.size or 1)
        except Exception:                                # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # TIER 2 - the vision LLM, only when tier 1 is unsure
    # ------------------------------------------------------------------
    def _escalate(self, obs: Observation, goal: Goal | None,
                  fast: GameState) -> GameState | None:
        """Ask a vision model to classify the screen. None if unavailable."""
        if self.vision is None or not obs.screenshot_path:
            return None

        allowed = ", ".join(s.value for s in ScreenType)
        target_line = (f"The run is trying to reach: {goal.target!r}.\n"
                       if goal and goal.target else "")
        question = (
            f"Classify this Android screenshot of Xbox Cloud Gaming.\n"
            f"{target_line}"
            f"A cheap text-based pass guessed '{fast.screen_type.value}' but was "
            f"only {fast.confidence:.0%} confident, so read the image itself.\n\n"
            f"Answer with EXACTLY these lines and nothing else:\n"
            f"SCREEN: <one of: {allowed}>\n"
            f"FOCUS: <the text of the highlighted/selected element, or NONE>\n"
            f"TARGET_VISIBLE: <yes|no>\n"
            f"TARGET_FOCUSED: <yes|no>\n"
            f"LOADING: <yes|no>\n"
            f"OVERLAY: <yes|no>\n"
            f"ERROR: <yes|no>\n"
            f"CONFIDENCE: <0.0-1.0>\n"
            f"WHY: <one sentence citing what you can SEE>\n\n"
            f"Rules: a mostly black frame mid-handoff is fullscreen_transition. "
            f"An Xbox loading animation or 'starting your game' is game_loading. "
            f"Browser chrome is normal and is not an error. If you cannot tell, "
            f"answer unknown with a low confidence rather than guessing."
        )

        try:
            reply = self.vision.describe(obs.screenshot_path, question)
        except Exception as exc:                         # noqa: BLE001
            log.debug(f"state escalation failed: {exc}", indent=2)
            return None
        if not reply:
            return None

        parsed = self._parse(reply)
        if "screen" not in parsed:
            return None

        try:
            screen = ScreenType(parsed["screen"])
        except ValueError:
            return None

        state = GameState(
            observation=obs,
            source="vision_llm",
            screen_type=screen,
            visible_text=fast.visible_text,
            application=fast.application,
        )

        focus_text = parsed.get("focus", "").strip()
        if focus_text and focus_text.lower() not in ("none", "n/a", "unknown"):
            state.focus = Focus(element=focus_text, confidence=0.8)

        yes = lambda key: parsed.get(key, "").strip().lower().startswith("y")  # noqa: E731
        state.target_visible = yes("target_visible")
        state.target_focused = yes("target_focused")
        state.loading = yes("loading") or screen in (
            ScreenType.GAME_LOADING, ScreenType.GAME_CONNECTING)
        state.overlay_present = yes("overlay")
        state.error_present = yes("error") or screen in (
            ScreenType.STREAM_ERROR, ScreenType.SESSION_EXPIRED)
        state.controller_prompt = screen is ScreenType.CONTROLLER_PROMPT
        state.game_running = screen in (
            ScreenType.LIVE_GAME_STREAM, ScreenType.GAME_MAIN_MENU,
            ScreenType.GAME_PAUSE_MENU, ScreenType.IN_GAME,
            ScreenType.GAME_SPLASH, ScreenType.PRESS_ANY_BUTTON)

        try:
            confidence = float(parsed.get("confidence", "0.5"))
        except ValueError:
            confidence = 0.5
        # Cap below certainty. A single model reading of a compressed cloud
        # stream is good evidence, not proof, and leaving room below 1.0 keeps
        # the confidence bands meaningful.
        state.confidence = round(max(0.0, min(confidence, 0.95)), 3)

        state.evidence = [
            f"vision LLM classified this as {screen.value}: "
            + parsed.get("why", "no reason given"),
            f"the fast text pass had guessed {fast.screen_type.value} at "
            f"{fast.confidence:.0%} confidence",
        ]
        return state

    @staticmethod
    def _parse(reply: str) -> dict[str, str]:
        """Read the KEY: value block back. Tolerant of extra prose.

        Models add a friendly preamble no matter how firmly they are asked not
        to, so this scans for the keys it wants and ignores everything else
        rather than requiring an exact format.
        """
        out: dict[str, str] = {}
        wanted = {"screen", "focus", "target_visible", "target_focused",
                  "loading", "overlay", "error", "confidence", "why"}
        for line in reply.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower().lstrip("*- ").strip()
            if key in wanted and key not in out:
                out[key] = value.strip().strip("*` ")
        return out
