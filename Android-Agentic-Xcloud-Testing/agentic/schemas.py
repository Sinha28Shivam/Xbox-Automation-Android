"""
schemas.py - the contracts between agents.

Agents talk to each other ONLY through these pydantic models. Two reasons:

1. An LLM asked for `TestPlan` returns a validated object or raises. The
   alternative - parsing prose with regexes - is how "dynamic" quietly becomes
   "unpredictable", and a test framework that lies is worse than none.

2. The graph state stays inspectable. Every field below ends up in the JSON
   report, so a failure can be re-read months later without the hardware.

NOTE on control names: no schema here enumerates buttons. The valid set is
whatever ../config/controls.yaml contains, discovered at runtime and injected
into the prompt. Hardcoding an enum would silently break the moment someone
adds a macro - and the parent project's whole point is that the YAML is the
source of truth.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ==========================================================================
# Enums
# ==========================================================================
class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"      # could not even start (no pad, no phone)
    INCONCLUSIVE = "inconclusive"   # ran, but we cannot honestly say
    ERROR = "error"          # the harness itself broke


class ActionKind(str, Enum):
    """What an executor step does. Mirrors pad_link.py's public API plus the
    observation/ADB verbs, and nothing more - the tool registry is the wall."""
    PRESS = "press"           # press a button/trigger N times
    HOLD = "hold"             # hold for a duration
    STICK = "stick"           # move an analog stick
    TRIGGER = "trigger"       # analog trigger 0..255
    MACRO = "macro"           # a named macro from controls.yaml
    SPECIAL = "special"       # a named special_action from controls.yaml
    RESET = "reset"           # release everything
    WAIT = "wait"             # sleep
    OBSERVE = "observe"       # screenshot + OCR + describe, no input
    LAUNCH_PWA = "launch_pwa"  # open the xCloud URL in a browser
    ADB_TEXT = "adb_text"     # inject text into focused input field via ADB
    ADB_KEYEVENT = "adb_keyevent"  # inject keycode (e.g. 66=ENTER) via ADB
    ASSERT = "assert"         # check an expectation against observation


class CauseClass(str, Enum):
    """RCA taxonomy. `retryable_causes` in agentic.yaml refers to these.

    Deliberately split by WHERE the fault is, because that is what decides the
    next action: a `wiring` fault will not fix itself on retry, a `timing` one
    often will.
    """
    WIRING = "wiring"                 # UART/GND/OTG - physical link
    FIRMWARE = "firmware"             # board answered wrongly or not at all
    HID_ENUMERATION = "hid_enumeration"   # phone never saw a gamepad
    HOST_MODE = "host_mode"           # phone not powering the board (OTG)
    PWA_NOT_LOADED = "pwa_not_loaded"  # browser/page never reached xCloud
    AUTH = "auth"                     # not signed in / session expired
    NETWORK = "network"
    STREAM_LATENCY = "stream_latency"
    TIMING = "timing"                 # input sent before the UI was ready
    FLAKY_UI = "flaky_ui"
    SCENARIO_DEFECT = "scenario_defect"   # the test itself is wrong/ambiguous
    HARNESS_DEFECT = "harness_defect"     # our bug
    APP_DEFECT = "app_defect"         # genuine xCloud bug - the real find
    UNKNOWN = "unknown"


# ==========================================================================
# Closed-loop state taxonomy
# ==========================================================================
class ScreenType(str, Enum):
    """WHAT kind of screen we are looking at - one value, not a set of flags.

    The single most important property here is that it is EXACTLY ONE value.
    The previous design carried independent booleans (`detail_page_open`,
    `stream_active`, `main_menu_visible`) which could all be true at once, so
    there was no such thing as "the current state" - only a bag of competing
    guesses. That made `state_before -> action -> state_after` inexpressible,
    and transition verification is impossible without it.

    Names are generic on purpose. A new game must be addable without touching
    this enum: `GAME_MAIN_MENU` is the state whether the game is Minecraft
    Dungeons or Forza. Game-specific meaning belongs in the scenario YAML.
    """
    UNKNOWN = "unknown"

    # Getting to xCloud
    ANDROID_HOME = "android_home"
    BROWSER = "browser"
    XCLOUD_HOME = "xcloud_home"
    XCLOUD_LIBRARY = "xcloud_library"

    # Choosing a game
    GAME_FOCUSED = "game_focused"
    GAME_DETAIL = "game_detail"

    # Launching - the states whose absence caused the run-log failure this
    # whole taxonomy exists to fix. Pressing A on a tile may land in ANY of
    # these, and none of them is an error.
    FULLSCREEN_TRANSITION = "fullscreen_transition"
    GAME_LOADING = "game_loading"
    GAME_CONNECTING = "game_connecting"

    # In the stream
    LIVE_GAME_STREAM = "live_game_stream"
    GAME_SPLASH = "game_splash"
    PRESS_ANY_BUTTON = "press_any_button"

    # Inside the game
    GAME_MAIN_MENU = "game_main_menu"
    GAME_PAUSE_MENU = "game_pause_menu"
    GAME_SETTINGS = "game_settings"
    IN_GAME = "in_game"

    # Things that steal gamepad input
    DIALOG = "dialog"
    OVERLAY = "overlay"
    KEYBOARD = "keyboard"
    CONTROLLER_PROMPT = "controller_prompt"

    # Things that end a run
    LOGIN = "login"
    SESSION_EXPIRED = "session_expired"
    QUEUE = "queue"
    NETWORK_WAIT = "network_wait"
    STREAM_ERROR = "stream_error"


# States in which sending input is pointless or actively harmful: the correct
# action is to WAIT. Defined here rather than in the decision agent so the
# verifier and the recovery agent read the same list - three copies of this
# set drifting apart is how "press A twice on a loading screen" happens.
WAITING_STATES: frozenset[ScreenType] = frozenset({
    ScreenType.FULLSCREEN_TRANSITION,
    ScreenType.GAME_LOADING,
    ScreenType.GAME_CONNECTING,
    ScreenType.QUEUE,
    ScreenType.NETWORK_WAIT,
    ScreenType.GAME_SPLASH,
})

# States that are terminal failures for any goal.
FATAL_STATES: frozenset[ScreenType] = frozenset({
    ScreenType.STREAM_ERROR,
    ScreenType.SESSION_EXPIRED,
})


class TransitionClass(str, Enum):
    """The verdict on ONE action, replacing a bare MET/NOT_MET.

    `INTERMEDIATE` is the member that matters. Without it, a correct launch
    (A on a focused tile -> FULLSCREEN_TRANSITION -> GAME_LOADING) is
    guaranteed to be recorded as a failure and to burn a full RCA cycle,
    because the only thing the old two-valued judgement could say about "not
    the screen I predicted" was "wrong".

    `UNKNOWN` is preserved from the old `expectation_met: None` and carries the
    same meaning: the evidence cannot settle it. It is NOT a pass.
    """
    SUCCESS = "success"           # the goal state, or the expected next state
    INTERMEDIATE = "intermediate"  # valid progress; usually means WAIT
    FAILURE = "failure"           # demonstrably wrong, or nothing happened
    UNKNOWN = "unknown"           # cannot tell - never treated as success


class FailureClass(str, Enum):
    """WHY an action failed, which decides what recovery to attempt.

    Distinct from `CauseClass`: that one names the LAYER at fault for the
    report (wiring, firmware, app). This one names the immediate, recoverable
    situation. `UI_NOT_READY` is a two-second wait; `WIRING` is the end of the
    run. Conflating them is what sent a previous run's reader after a USB-OTG
    problem that did not exist.
    """
    NONE = "none"
    UI_NOT_READY = "ui_not_ready"
    FOCUS_WRONG = "focus_wrong"
    OVERLAY_PRESENT = "overlay_present"
    DIALOG_PRESENT = "dialog_present"
    KEYBOARD_PRESENT = "keyboard_present"
    STILL_LOADING = "still_loading"
    # The firmware queued the report and NEITHER look saw the screen move.
    # This is the old `silent_failure`, given a name.
    INPUT_IGNORED = "input_ignored"
    WRONG_ACTION = "wrong_action"
    VISION_UNCERTAIN = "vision_uncertain"
    STATE_UNKNOWN = "state_unknown"
    GOAL_NOT_REACHED = "goal_not_reached"
    STREAM_ERROR = "stream_error"
    SESSION_EXPIRED = "session_expired"
    QUEUE_TIMEOUT = "queue_timeout"
    CONTROLLER_NOT_DETECTED = "controller_not_detected"


# Which failure classes a cheap, LLM-free recovery can plausibly fix. Anything
# outside this set goes straight to RCA rather than being retried in hope.
RECOVERABLE_FAILURES: frozenset[FailureClass] = frozenset({
    FailureClass.UI_NOT_READY,
    FailureClass.FOCUS_WRONG,
    FailureClass.OVERLAY_PRESENT,
    FailureClass.DIALOG_PRESENT,
    FailureClass.STILL_LOADING,
    FailureClass.VISION_UNCERTAIN,
    FailureClass.STATE_UNKNOWN,
    # These two are here for ONE specific reason: a browser hides a gamepad it
    # has not heard from (the W3C Gamepad API requires a button event before
    # `navigator.getGamepads()` will report a pad). When a page loses the pad -
    # a reloaded tab, a stream handoff, an idle timeout - the symptom is exactly
    # INPUT_IGNORED: the firmware queues the report, and nothing moves.
    #
    # That is indistinguishable from a wiring fault by inspection, and it used
    # to be diagnosed as one. Re-running the signal handshake costs about two
    # seconds and no LLM call, so it is worth trying BEFORE concluding the
    # hardware is broken.
    #
    # NOTE: `silent_failure` is still set on that step, and the evaluator's
    # ceiling still turns it into FAIL. That is deliberate. A page that stopped
    # accepting input mid-run IS a finding worth failing over, even if the
    # handshake then recovered it - the recovery lets the run continue and
    # gather more evidence, it does not erase what happened.
    FailureClass.INPUT_IGNORED,
    FailureClass.CONTROLLER_NOT_DETECTED,
})



# ==========================================================================
# Scenario understanding (ScenarioAgent)
# ==========================================================================

class AcceptanceCriterion(BaseModel):
    """One observable, checkable statement extracted from the scenario."""
    id: str = Field(description="short stable id, e.g. 'ac1'")
    statement: str = Field(description="what must be true, in one sentence")
    observable_via: list[str] = Field(
        default_factory=list,
        description="how it can be checked: screen_text, screen_change, "
                    "pad_state, logcat, focused_window")
    critical: bool = Field(
        default=True,
        description="false = nice-to-have; a miss degrades but does not fail")


class ScenarioSpec(BaseModel):
    """A free-text scenario, understood.

    The input may be a YAML file, a markdown paragraph, or one sentence typed
    at the CLI. This is the normalised form everything downstream reads.
    """
    title: str
    intent: str = Field(description="one paragraph: what is being verified")
    preconditions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)

    is_testable: bool = Field(
        default=True,
        description="false when the scenario cannot be verified with the "
                    "available capabilities - say so BEFORE burning a run")
    ambiguities: list[str] = Field(
        default_factory=list,
        description="things a human must decide; each one weakens the verdict")
    clarified_assumptions: list[str] = Field(
        default_factory=list,
        description="assumptions taken to proceed despite an ambiguity")
    risk_notes: list[str] = Field(default_factory=list)
    estimated_steps: int = Field(default=0, ge=0)


# ==========================================================================
# Device / environment (DeviceAgent)
# ==========================================================================
class PadStatus(BaseModel):
    """Result of the hardware handshake, straight from pad_link.PadLink."""
    link_open: bool = False
    port: str | None = None
    firmware: str | None = None
    transport: str | None = None
    # From `PONG <fw> <transport> <connected>`: whether a USB HOST (the phone)
    # has enumerated the pad. The single most diagnostic bit we have.
    pad_connected_to_phone: bool | None = None
    dry_run: bool = False
    error: str | None = None
    diagnostics: list[str] = Field(default_factory=list)


class AndroidStatus(BaseModel):
    """What we could learn about the phone. All optional - adb may be absent."""
    adb_available: bool = False
    adb_version: str | None = None
    device_serial: str | None = None
    device_state: str | None = None      # device | unauthorized | offline
    model: str | None = None
    android_version: str | None = None
    sdk: str | None = None
    screen_size: str | None = None
    screen_on: bool | None = None
    focused_window: str | None = None
    # xCloud is a PWA: there is no xCloud package. We report the browsers we
    # actually found and any WebAPK that looks like it, and let the agents
    # reason from that rather than assuming a package name.
    browsers_found: list[str] = Field(default_factory=list)
    webapks_found: list[str] = Field(default_factory=list)
    chosen_launcher: str | None = None
    error: str | None = None


class Capabilities(BaseModel):
    """What this run can actually do. Injected into every prompt.

    This is the anti-hallucination mechanism: the model is told the real list of
    buttons, macros and sensors, so it cannot invent `press("options")`.
    """
    buttons: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    sticks: dict[str, list[str]] = Field(default_factory=dict)
    macros: dict[str, str] = Field(default_factory=dict)
    special_actions: dict[str, str] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)
    timing: dict[str, float] = Field(default_factory=dict)

    can_send_input: bool = False
    can_screenshot: bool = False
    can_read_text: bool = False       # OCR present
    can_read_logs: bool = False
    can_launch_pwa: bool = False
    can_adb_text: bool = False        # ADB text injection into focused fields
    vision_model: bool = False        # the LLM profile can see images

    unavailable: list[str] = Field(
        default_factory=list,
        description="capability -> why it is missing, for the report")

    def summary_for_prompt(self) -> str:
        """Compact, model-friendly rendering. Kept here so every agent shows
        the model the SAME picture of the world."""
        lines = [
            f"buttons: {', '.join(self.buttons) or 'none'}",
            f"triggers: {', '.join(self.triggers) or 'none'}",
            "sticks: " + (", ".join(
                f"{n}({'/'.join(d)})" for n, d in self.sticks.items()) or "none"),
        ]
        if self.macros:
            lines.append("macros: " + ", ".join(
                f"{n} - {d}" for n, d in self.macros.items()))
        if self.special_actions:
            lines.append("special_actions: " + ", ".join(self.special_actions))
        sensors = [n for n, ok in (
            ("send_input", self.can_send_input),
            ("screenshot", self.can_screenshot),
            ("ocr_text", self.can_read_text),
            ("logs", self.can_read_logs),
            ("launch_pwa", self.can_launch_pwa),
            ("adb_text", self.can_adb_text),
            ("vision_model", self.vision_model),
        ) if ok]
        lines.append(f"available sensors/actuators: {', '.join(sensors) or 'none'}")
        if self.unavailable:
            lines.append("NOT available: " + "; ".join(self.unavailable))
        return "\n".join(lines)


class EnvironmentReport(BaseModel):
    """DeviceAgent's verdict on whether we may proceed at all."""
    pad: PadStatus = Field(default_factory=PadStatus)
    android: AndroidStatus = Field(default_factory=AndroidStatus)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    ready: bool = False
    guide_signal_verified: bool | None = None
    guide_verification_notes: str = ""
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # The LLM's plain-English read of the above, including the fix to try.
    assessment: str = ""


# ==========================================================================
# Planning (PlannerAgent)
# ==========================================================================
class PlanStep(BaseModel):
    """One executable step. Field names mirror pad_link.py's arguments so the
    executor is a thin, auditable dispatch - no clever translation layer to be
    wrong in."""
    id: str
    kind: ActionKind
    # `target` means: button/trigger/stick name, macro name, or url.
    target: str | None = None
    times: int = Field(default=1, ge=1)
    duration: float | None = None
    interval: float | None = None
    value: int | None = None          # trigger 0..255
    direction: str | None = None      # stick direction
    x: int | None = None
    y: int | None = None
    seconds: float | None = None      # for WAIT

    intent: str = Field(default="", description="why this step exists")
    expectation: str = Field(
        default="",
        description="what should be observable AFTER this step. Empty means "
                    "'no check' - honest, but it weakens the verdict.")
    criterion_ids: list[str] = Field(
        default_factory=list,
        description="acceptance criteria this step contributes evidence to")
    optional: bool = Field(
        default=False,
        description="a failure here does not fail the run (e.g. a wake press)")
    observe_after: bool = True


class TestPlan(BaseModel):
    steps: list[PlanStep] = Field(default_factory=list)
    strategy: str = Field(default="", description="the approach, in prose")
    assumptions: list[str] = Field(default_factory=list)
    # Filled by the planner when it is asked to REPLAN after an RCA.
    revision: int = Field(default=1, ge=1)
    replan_reason: str | None = None


# ==========================================================================
# Observation (ObserverAgent)
# ==========================================================================
class Observation(BaseModel):
    """One look at the world, after (or before) a step."""
    step_id: str | None = None
    timestamp: float = 0.0
    screenshot_path: str | None = None
    screen_text: str = ""                # OCR
    screen_description: str = ""         # vision LLM
    ui_elements: list[str] = Field(default_factory=list)
    # Fraction of pixels changed vs the previous frame. THE check the parent
    # project lacked: it can say "the input did nothing", which an `OK` cannot.
    change_ratio: float | None = None
    screen_changed: bool | None = None
    focused_window: str | None = None
    focused_tile: str | None = None      # name/text of currently highlighted tile
    visible_tiles: list[str] = Field(default_factory=list)
    target_visible: bool = False         # target game tile is visible on screen
    target_focused: bool = False         # target game tile is currently focused
    detail_page_open: bool = False       # game detail page ("Play" / "Play now") is open
    stream_active: bool = False          # live game stream video is active
    main_menu_visible: bool = False      # in-game main menu reached
    pad_state: str | None = None         # the board's own view of what it holds
    log_excerpt: str = ""
    notes: list[str] = Field(default_factory=list)
    sensors_used: list[str] = Field(default_factory=list)
    degraded: bool = Field(
        default=False,
        description="true when a sensor we wanted was unavailable")


# ==========================================================================
# Structured world state (StateBuilder) - the closed loop's source of truth
# ==========================================================================
class Focus(BaseModel):
    """WHICH element the UI highlight is on, and how sure we are.

    Carries its own confidence because "I can see a focus ring but cannot read
    the label" is a real and common situation, and it must not be reported as
    "nothing is focused" - those lead to opposite actions.
    """
    element: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class GameState(BaseModel):
    """The world, as one structured value. The unit the agent reasons over.

    `Observation` (above) stays exactly what it was: the RAW sensor record -
    pixels, OCR text, a change ratio, log lines. This is the INTERPRETATION of
    one or more of those. Keeping them separate is deliberate: the observation
    is a fact and can be re-read later, while this is a judgement that a better
    perception pass might revise.

    `evidence` and `source` are not decoration. Every other judgement in this
    project cites what it relied on, and a GameState that asserted
    "GAME_LOADING" without saying why would be the one unauditable object in
    the system. `source` additionally lets the report show how many states were
    resolved cheaply versus by a vision-LLM call, which is the only honest way
    to verify that the fast-perception tier is actually saving anything.
    """
    application: str = Field(
        default="unknown",
        description="'xcloud', or the game's id once it is running")
    screen_type: ScreenType = ScreenType.UNKNOWN
    visible_text: list[str] = Field(default_factory=list)
    focus: Focus = Field(default_factory=Focus)

    loading: bool = False
    overlay_present: bool = False
    controller_prompt: bool = False
    error_present: bool = False
    game_running: bool = False

    # Does the goal's target appear at all / is it focused? Derived from the
    # scenario's target string, never from a hardcoded game name.
    target_visible: bool = False
    target_focused: bool = False

    # PERCEPTION confidence: how sure we are that `screen_type` is right. This
    # is NOT the same as a judgement's confidence, and conflating the two is
    # why the old design could not implement "re-observe when unsure": it only
    # ever recorded how sure the judge was about its verdict, never how sure
    # the eyes were about the picture.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    evidence: list[str] = Field(
        default_factory=list,
        description="WHY this classification - quoted OCR, ratios, cues")
    source: Literal["fast", "vision_llm", "merged", "none"] = "none"
    observation: Observation | None = None

    def is_waiting_state(self) -> bool:
        """True when the right move is to wait, not to press something."""
        return self.screen_type in WAITING_STATES or self.loading

    def is_fatal(self) -> bool:
        return self.screen_type in FATAL_STATES or self.error_present

    def summary(self) -> str:
        """One line for the log and for prompts."""
        bits = [f"screen={self.screen_type.value}"]
        if self.focus.element:
            bits.append(f"focus={self.focus.element!r}")
        if self.target_focused:
            bits.append("target_FOCUSED")
        elif self.target_visible:
            bits.append("target_visible")
        for name, flag in (("loading", self.loading),
                           ("overlay", self.overlay_present),
                           ("error", self.error_present),
                           ("game_running", self.game_running)):
            if flag:
                bits.append(name)
        bits.append(f"confidence={self.confidence:.0%}")
        bits.append(f"via={self.source}")
        return " ".join(bits)


# ==========================================================================
# Actions (DecisionAgent) - what the model is allowed to ask for
# ==========================================================================
class ActionType(str, Enum):
    PRESS = "press"
    HOLD = "hold"
    STICK = "stick"
    TRIGGER = "trigger"
    MACRO = "macro"
    WAIT = "wait"
    OBSERVE = "observe"
    RESET = "reset"
    LAUNCH_PWA = "launch_pwa"
    # An explicit "the goal is reached, stop" so the agent can end a run by
    # deciding to, rather than by running out of iterations.
    DONE = "done"


class Action(BaseModel):
    """ONE action. The canonical form the decision agent emits.

    Deliberately NOT the same model as `PlanStep`. A PlanStep is an entry in a
    pre-written script and carries scheduling baggage (`criterion_ids`,
    `optional`, `observe_after`). An Action is a single live decision made from
    the state in front of us. `to_plan_step()` bridges them so the existing,
    hardware-proven `PadTool.dispatch()` is reused untouched.

    `times` is capped at 1 by the field constraint, not by a later fix-up. In
    the closed loop, "press right three times" is not a thing you can ask for:
    the number of presses is an OUTPUT of observing where the focus went, so
    the request is unrepresentable rather than corrected after the fact.
    """
    type: ActionType = ActionType.OBSERVE
    control: str | None = Field(
        default=None,
        description="button/trigger/stick/macro name from the capability list")
    duration: float | None = None
    x: int | None = None
    y: int | None = None
    direction: str | None = None
    value: int | None = None
    seconds: float | None = Field(default=None, description="for WAIT")
    times: int = Field(
        default=1, ge=1, le=1,
        description="ALWAYS 1. Closed-loop control observes between presses.")

    rationale: str = Field(
        default="",
        description="why THIS action, given the state - cite the state")
    expected_states: list[ScreenType] = Field(
        default_factory=list,
        description="the states this action may legitimately produce")

    def describe(self) -> str:
        bits = [self.type.value]
        if self.control:
            bits.append(self.control)
        if self.direction:
            bits.append(f"dir={self.direction}")
        if self.seconds is not None:
            bits.append(f"{self.seconds:.2f}s")
        elif self.duration is not None:
            bits.append(f"{self.duration:.2f}s")
        return " ".join(bits)

    def to_plan_step(self, step_id: str) -> PlanStep:
        """Bridge to the existing executor/pad dispatch path.

        Reusing PlanStep here rather than teaching PadTool a second vocabulary
        keeps exactly one place that knows how to talk to the board.
        """
        mapping = {
            ActionType.PRESS: ActionKind.PRESS,
            ActionType.HOLD: ActionKind.HOLD,
            ActionType.STICK: ActionKind.STICK,
            ActionType.TRIGGER: ActionKind.TRIGGER,
            ActionType.MACRO: ActionKind.MACRO,
            ActionType.WAIT: ActionKind.WAIT,
            ActionType.OBSERVE: ActionKind.OBSERVE,
            ActionType.RESET: ActionKind.RESET,
            ActionType.LAUNCH_PWA: ActionKind.LAUNCH_PWA,
            ActionType.DONE: ActionKind.OBSERVE,
        }
        return PlanStep(
            id=step_id,
            kind=mapping.get(self.type, ActionKind.OBSERVE),
            target=self.control,
            times=1,
            duration=self.duration,
            value=self.value,
            direction=self.direction,
            x=self.x,
            y=self.y,
            seconds=self.seconds,
            intent=self.rationale,
            expectation=(
                "one of: " + ", ".join(s.value for s in self.expected_states)
                if self.expected_states else ""),
        )


# ==========================================================================
# Goals (from the scenario) and transitions (the verifier)
# ==========================================================================
class Goal(BaseModel):
    """What this run is trying to reach, in terms of STATES not keystrokes.

    Parsed from the scenario YAML's own `state_model` / `judge_policy` blocks.
    `allowed_transitions` is the direct fix for the failure that motivated all
    of this: GAME_FOCUSED + A may legitimately produce a detail page OR a
    fullscreen transition OR a loading screen, and a rig that admits only one
    of those will report a successful launch as a failure.
    """
    description: str = ""
    target: str | None = Field(
        default=None, description="e.g. the game name to navigate to")
    success_states: list[ScreenType] = Field(default_factory=list)
    failure_states: list[ScreenType] = Field(default_factory=list)
    intermediate_states: list[ScreenType] = Field(default_factory=list)
    allowed_transitions: dict[str, list[ScreenType]] = Field(
        default_factory=dict,
        description="state_before(value) -> the states it may lead to")
    max_iterations: int = Field(default=40, ge=1)

    def is_success(self, state: GameState) -> bool:
        return state.screen_type in self.success_states

    def is_failure(self, state: GameState) -> bool:
        return state.screen_type in self.failure_states


class Transition(BaseModel):
    """before + action + after, classified. The unit of progress.

    This replaces the question "does the screen look like what I predicted?"
    with "given where we were, what we sent, and where we are now, was that
    valid progress toward the goal?" - which is the only form of the question
    that can be answered correctly when several different next screens are all
    legitimate.
    """
    state_before: ScreenType = ScreenType.UNKNOWN
    state_after: ScreenType = ScreenType.UNKNOWN
    action: Action | None = None
    classification: TransitionClass = TransitionClass.UNKNOWN
    expected: list[ScreenType] = Field(default_factory=list)
    transition_valid: bool = False
    goal_complete: bool = False
    failure_class: FailureClass = FailureClass.NONE
    next_recommendation: str = Field(
        default="",
        description="WAIT / OBSERVE / CONTINUE / RECOVER / STOP")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)
    # Preserved from the pre-closed-loop executor. The firmware queued the
    # report and NEITHER look saw the screen move. Kept as its own flag rather
    # than folded into `failure_class` because the evaluator's verdict ceiling
    # keys off it directly, and that rule must not become dependent on how a
    # classifier happened to phrase things.
    silent_failure: bool = False

    def describe(self) -> str:
        act = self.action.describe() if self.action else "?"
        return (f"{self.state_before.value} --{act}--> "
                f"{self.state_after.value} = {self.classification.value}")


class StepResult(BaseModel):

    step: PlanStep

    dispatched: bool = False         # the command was accepted by the board
    hardware_ok: bool = False        # firmware replied OK
    expectation_met: bool | None = None   # None = not checked / unknowable
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning: str = ""
    # The SETTLED look: taken after the animation finished. This is the state a
    # judgement about "what does the screen show now" must be made against.
    observation: Observation | None = None
    # The GLANCE: taken ~0.45s after the input, before the UI settles.
    #
    # Run 20260817-105323 is why this field exists. Its nav_test moved the screen
    # 3.257%, then the only observation that counted was taken after the highlight
    # had settled back (0.074%) and the run was recorded as a FAIL. A transient
    # reaction proves the input arrived exactly as well as a persistent one, so
    # the evidence must be captured before it evaporates, not only after.
    glance_observation: Observation | None = None
    # Which of the two looks (if either) showed motion. Kept as a field rather
    # than derived, because the evaluator and the RCA agent both need to cite it.
    reacted_on: Literal["glance", "settle", "both", "neither", "unknown"] = \
        "unknown"
    # How long this step deliberately waited, and why. Makes "the run took 338s"
    # answerable from the report instead of from screenshot timestamps.
    waited_seconds: float = 0.0
    settle_profile: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    # True when hardware said OK but nothing visibly happened - the exact trap
    # documented in the parent README ("Commands say ok but phone does nothing").
    #
    # NOTE the strengthened bar: this is only set when NEITHER look saw motion.
    # Under the old single-look rule it could fire on a step whose evidence had
    # simply been photographed too late, which is a harness defect wearing the
    # costume of a hardware fault - the most expensive kind of wrong answer.
    silent_failure: bool = False

    # -- closed-loop fields ------------------------------------------------
    # All optional, so a legacy plan-mode run produces exactly the same
    # StepResult it always did and the reporter/RCA need no special-casing.
    action: Action | None = Field(
        default=None,
        description="the decided Action, when this step came from the "
                    "closed loop rather than from a pre-written plan")
    game_state_before: GameState | None = None
    game_state_after: GameState | None = None
    transition: Transition | None = None
    iteration: int = Field(
        default=0,
        description="which closed-loop pass produced this; 0 = plan mode")
    recovery_attempt: int = Field(
        default=0,
        description="how many recoveries had been tried for this goal already")


# ==========================================================================
# Evaluation (EvaluatorAgent)
# ==========================================================================

class CriterionResult(BaseModel):
    criterion_id: str
    statement: str
    met: bool | None = None          # None = no evidence either way
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    reasoning: str = ""


class Evaluation(BaseModel):
    verdict: Verdict = Verdict.INCONCLUSIVE
    criteria: list[CriterionResult] = Field(default_factory=list)
    summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # Anything that makes the verdict less trustworthy than it looks.
    caveats: list[str] = Field(default_factory=list)


# ==========================================================================
# RCA (RootCauseAgent)
# ==========================================================================
class Hypothesis(BaseModel):
    cause_class: CauseClass = CauseClass.UNKNOWN
    statement: str = ""
    likelihood: float = Field(default=0.0, ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    # A check that could DISPROVE this - the discipline that cracked the
    # parent project (verify_hid_raw.py was "a check capable of saying no").
    discriminating_test: str = ""


class RootCauseAnalysis(BaseModel):
    primary: Hypothesis = Field(default_factory=Hypothesis)
    alternatives: list[Hypothesis] = Field(default_factory=list)
    failure_step_id: str | None = None
    layer: Literal["scenario", "harness", "wiring", "firmware", "phone",
                   "browser_pwa", "network", "xcloud", "unknown"] = "unknown"
    narrative: str = ""
    recommendations: list[str] = Field(default_factory=list)
    retryable: bool = False
    retry_strategy: str | None = None


# ==========================================================================
# Reporting (ReporterAgent)
# ==========================================================================
class TestReport(BaseModel):
    run_id: str
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    scenario: ScenarioSpec | None = None
    environment: EnvironmentReport | None = None
    plan: TestPlan | None = None
    step_results: list[StepResult] = Field(default_factory=list)
    evaluation: Evaluation | None = None
    root_cause: RootCauseAnalysis | None = None
    verdict: Verdict = Verdict.INCONCLUSIVE
    executive_summary: str = ""
    recommendations: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    replans: int = 0
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)
