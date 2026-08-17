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
