"""
scenario.py - AGENT 2: understand and VERIFY the test scenario.

Input: whatever the user wrote. A YAML file, a markdown paragraph, or one
sentence typed at the CLI. No fixed grammar - that is the "nothing hardcoded"
requirement taken seriously.

Output: a ScenarioSpec with acceptance criteria that are OBSERVABLE with the
capabilities this particular run has.

WHY VERIFICATION IS ITS OWN AGENT
---------------------------------
Most scenario failures are not device failures. "Check the stream looks good" is
not testable by this rig; neither is anything needing audio, or a specific saved
game. Discovering that AFTER a 4-minute hardware run wastes the run and, worse,
tempts the system into inventing a verdict for a question it never checked.

So this agent's real job is to be willing to say NO:
  * is_testable=False           -> the graph stops before touching hardware
  * ambiguities                 -> recorded; each one weakens the final verdict
  * observable_via per criterion-> ties a criterion to a sensor that EXISTS

A criterion whose `observable_via` names a sensor we do not have is not a
criterion, it is a wish. It is kept, marked, and reported as unverifiable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..schemas import AcceptanceCriterion, ScenarioSpec
from ..state import GraphState
from .base import Agent

ROLE = """\
You turn a free-text test scenario into a precise, checkable specification.

Rules:
1. Extract acceptance criteria that are OBSERVABLE with the capabilities listed
   below - nothing else. For each, set `observable_via` using only these values:
     screen_text      - text visible on screen (needs OCR)
     screen_change    - the screen changed after an input (needs screenshots)
     pad_state        - what the board reports it is holding
     logcat           - Android log lines
     focused_window   - which app/browser is in front
2. If a criterion cannot be checked with the available sensors, still list it but
   set critical=false and name the problem in `risk_notes`. Do not silently drop
   it and do not pretend a different check proves it.
3. Set is_testable=false ONLY when the core of the scenario could never be
   verified by ANY amount of looking at the screen - it needs audio, frame-exact
   timing, a specific save file, or a subjective judgement like image quality.
   Refusing a run is a valid and useful outcome, but it is a strong claim.

   UI VARIABILITY IS NOT A REASON TO REFUSE. All of the following are NORMAL and
   must NOT set is_testable=false:
     - "the exact wording may vary" (Play / Play now / Resume)
     - "the tile's appearance or focus indicator may vary by xCloud version"
     - "which rail the game appears in is not specified"
     - "loading time varies with network conditions"
     - "the game's menu layout may differ by version"

   The runner OBSERVES the screen and decides its next action from what is
   actually there, one action at a time. It does not follow a pre-written
   keystroke route, so it does not need to be told in advance what the screen
   will look like - discovering that is its job. A scenario that says "reach the
   Minecraft Dungeons main menu" is fully testable even though nobody can say
   ahead of time how many D-pad presses that takes or exactly what the menu
   says. Record each such point in `clarified_assumptions` and continue.
4. Vague scenarios are normal. Do not invent detail: record the ambiguity, state
   the assumption you would proceed with in `clarified_assumptions`, and continue.

5. `estimated_steps` is your honest guess at the number of gamepad actions
   needed. Keep it proportionate - a menu check is a handful, not thirty.

Remember xCloud is a PWA in a browser: "open the app" means opening a URL, and
seeing browser UI is not a defect.
"""


class ScenarioAgent(Agent):
    name = "scenario"

    # -- input normalisation ----------------------------------------------
    @staticmethod
    def load_raw(source: str) -> tuple[str, str]:
        """Accept a path or literal text. Returns (text, source_label).

        Structured YAML is passed through as YAML text rather than being parsed
        into fields here: the model reads it perfectly well, and parsing would
        mean inventing a schema the user must then obey - exactly the hardcoding
        we are avoiding.
        """
        candidate = Path(source)
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8"), str(candidate)
        except OSError:
            pass
        return source, "<inline>"

    # -- main --------------------------------------------------------------
    def run(self, state: GraphState) -> GraphState:
        raw = state.get("raw_scenario", "") or ""
        if not raw.strip():
            spec = ScenarioSpec(
                title="empty scenario", intent="none given",
                is_testable=False,
                ambiguities=["no scenario text was provided at all"])
            return {"scenario": spec,
                    "halt_reason": "no scenario was provided",
                    "agent_trace": [self.trace("verify", "empty scenario")]}

        caps = state.get("capabilities")
        user = "\n".join([
            "SCENARIO AS WRITTEN (verbatim, may be YAML, markdown or prose):",
            "```",
            raw.strip(),
            "```",
            "",
            self.capability_block(state),
            "",
            "ENVIRONMENT NOTES:",
            self._environment_notes(state),
        ])

        spec: ScenarioSpec | None = self.think(
            ScenarioSpec, self.system_prompt(ROLE), user, default=None)

        interpreted = spec is not None
        if spec is None:
            spec = self._mechanical_spec(raw, state)

        # Cross-check the model against reality: it may have claimed a sensor we
        # do not have. Trust code over prose for what exists.
        self._reconcile_with_capabilities(spec, caps, interpreted)

        # ...and cross-check the REFUSAL, in code. A prompt rule is a request.
        self._reconsider_refusal(spec, caps)

        halt = None
        if not spec.is_testable:
            halt = ("the scenario is not testable with this rig: "
                    + "; ".join(spec.ambiguities or ["no reason given"]))


        return {
            "scenario": spec,
            "halt_reason": halt,
            "agent_trace": [self.trace(
                "verify",
                f"title={spec.title!r} testable={spec.is_testable} "
                f"criteria={len(spec.acceptance_criteria)} "
                f"ambiguities={len(spec.ambiguities)}")],
        }

    # -- helpers -----------------------------------------------------------
    def _environment_notes(self, state: GraphState) -> str:
        env = state.get("environment")
        if env is None:
            return "  (environment not probed yet)"
        lines = []
        if env.android.model:
            lines.append(f"  device: {env.android.model} "
                         f"(Android {env.android.android_version})")
        if env.android.browsers_found:
            lines.append("  browsers available for the PWA: "
                         + ", ".join(env.android.browsers_found))
        else:
            lines.append("  no browser was discovered on the device")
        if env.warnings:
            lines.append("  warnings: " + " | ".join(env.warnings))
        return "\n".join(lines) or "  (nothing notable)"

    def _reconcile_with_capabilities(self, spec: ScenarioSpec, caps: Any,
                                     interpreted: bool = True) -> None:
        """Demote criteria that depend on sensors this run does not have.

        `interpreted` says whether an LLM actually read the scenario. It matters
        for the final rule below: refusing to run is only a meaningful judgement
        if something judged. Without it we would refuse EVERY no-LLM run, when
        the useful behaviour is to run the capability probe and label the result
        honestly.
        """
        if caps is None:
            return
        sensor_available = {
            "screen_text": caps.can_read_text,
            "screen_change": caps.can_screenshot,
            "pad_state": caps.can_send_input,
            "logcat": caps.can_read_logs,
            "focused_window": caps.can_read_logs,
        }
        for crit in spec.acceptance_criteria:
            wanted = [v for v in crit.observable_via if v in sensor_available]
            if wanted and not any(sensor_available[v] for v in wanted):
                crit.critical = False
                spec.risk_notes.append(
                    f"criterion {crit.id} needs "
                    f"{', '.join(wanted)}, which is not available this run, so "
                    f"it cannot be verified - it is reported as unverified "
                    f"rather than passed")

        # If nothing critical survives, the run cannot produce a real verdict.
        # Better to say so now than to hand back a confident "pass" built on
        # zero evidence.
        #
        # Gated on `interpreted` deliberately. In the no-LLM path nothing is
        # critical because nothing was UNDERSTOOD, not because the sensors are
        # missing - so refusing here would block every offline run. Those runs
        # still have value: they prove whether input reaches xCloud at all. The
        # evaluator's verdict ceiling stops that being mistaken for a pass.
        if interpreted and spec.acceptance_criteria and not any(
                c.critical for c in spec.acceptance_criteria):
            spec.is_testable = False
            spec.ambiguities.append(
                "no acceptance criterion can be observed with the sensors "
                "available, so this run could not prove or disprove anything")
        elif not interpreted:
            spec.risk_notes.append(
                "the scenario was not interpreted, so the run proceeds as a "
                "capability probe only - it checks whether input reaches "
                "xCloud, NOT whether the scenario as written is satisfied")

    # Phrases that describe UI VARIABILITY, not untestability. A closed-loop
    # runner discovers all of these by looking at the screen, which is its whole
    # job, so none of them is a reason to refuse a run.
    _VARIABILITY_MARKERS = (
        "may vary", "might vary", "can vary", "varies",
        "not specified", "unspecified", "is not stated", "not defined",
        "exact wording", "exact text", "exact appearance", "exact layout",
        "exact visual", "may differ", "might differ", "could differ",
        "depends on version", "by version", "ui version", "pwa version",
        "network conditions", "server load", "may change",
        "which rail", "requires scrolling", "may require scrolling",
    )

    # Things that genuinely CANNOT be seen, however long you look. These are the
    # only honest reasons to refuse.
    _REAL_BLOCKERS = (
        "audio", "sound", "volume level", "frame-exact", "frame exact",
        "specific save", "saved game", "save file", "image quality",
        "subjective", "latency measurement", "input lag", "frame rate",
        "fps", "smoothness", "requires a second account", "two players",
        "haptic", "rumble", "vibration",
    )

    def _reconsider_refusal(self, spec: ScenarioSpec, caps: Any) -> None:
        """Overturn a refusal that rests only on UI variability.

        WHY THIS IS IN CODE
        -------------------
        The prompt already says variability is not a reason to refuse, but a
        prompt is a request. This is the wall - the same distinction this project
        draws everywhere else between asking a model nicely and enforcing a rule.

        It matters because the failure mode is severe and silent: the run halts
        at the SCENARIO node, so the pad is never touched, no screenshot is
        taken, and the report says "inconclusive - nothing was tested". A real
        run of a perfectly testable scenario is thrown away over the observation
        that xCloud's button might say "Play now" instead of "Play".

        The asymmetry is deliberate. A refusal citing only variability is
        overturned; a refusal citing anything in `_REAL_BLOCKERS` stands, and so
        does a refusal we cannot classify. Being unable to tell is not a licence
        to proceed.
        """
        if spec.is_testable or not spec.ambiguities:
            return

        blob = " ".join(spec.ambiguities).lower()

        # Any genuine blocker present? Then the refusal stands, whatever else it
        # also mentions - one unobservable requirement is enough.
        blockers = [m for m in self._REAL_BLOCKERS if m in blob]
        if blockers:
            spec.risk_notes.append(
                f"refusal upheld: the scenario needs something no camera can "
                f"see ({', '.join(blockers[:3])})")
            return

        # Every stated reason is variability?
        variability = [m for m in self._VARIABILITY_MARKERS if m in blob]
        if not variability:
            # Unclassifiable. Leave the refusal alone - a reason we do not
            # recognise is not a reason we may ignore.
            return

        # Overturn it, and move the reasons where they belong: they are
        # assumptions the run proceeds under, not grounds for refusing.
        spec.is_testable = True
        spec.clarified_assumptions.extend(spec.ambiguities)
        spec.ambiguities = []
        spec.risk_notes.append(
            "the scenario was initially judged untestable, but every reason "
            "given was UI VARIABILITY (wording, layout, which rail a game "
            "appears in, loading time). A closed-loop run resolves those by "
            "observing the screen and choosing one action at a time, so they "
            "are recorded as assumptions and the run proceeds. Refusing here "
            "would have halted before the pad was ever touched and reported "
            "'nothing was tested', which is the more misleading outcome.")

        # If the refusal stripped every critical criterion, restore criticality
        # for anything the sensors CAN actually observe - otherwise the run
        # proceeds but can never claim a pass, which is a refusal by another
        # name.
        if caps is not None and spec.acceptance_criteria and not any(
                c.critical for c in spec.acceptance_criteria):
            available = {
                "screen_text": caps.can_read_text,
                "screen_change": caps.can_screenshot,
                "pad_state": caps.can_send_input,
                "logcat": caps.can_read_logs,
                "focused_window": caps.can_read_logs,
            }
            restored = 0
            for crit in spec.acceptance_criteria:
                if any(available.get(v) for v in crit.observable_via):
                    crit.critical = True
                    restored += 1
            if restored:
                spec.risk_notes.append(
                    f"{restored} criterion(s) restored to critical because the "
                    f"sensors needed to check them ARE available this run")

    def _mechanical_spec(self, raw: str, state: GraphState) -> ScenarioSpec:

        """No-LLM fallback.

        Deliberately dumb: it does NOT try to guess intent from keywords. It
        preserves the text, lifts any structure the user already wrote (YAML
        keys, markdown bullets) into criteria, and flags the whole thing as
        needing review. Keyword-guessing here would be the exact kind of hidden
        hardcoding this design avoids.
        """
        title = "scenario"
        criteria: list[AcceptanceCriterion] = []
        bullets: list[str] = []

        # 1) The user may already have written structure - use theirs, not ours.
        try:
            parsed = yaml.safe_load(raw)
        except (yaml.YAMLError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            for key in ("title", "name", "scenario"):
                if isinstance(parsed.get(key), str):
                    title = parsed[key]
                    break
            for key in ("acceptance_criteria", "criteria", "expected",
                        "assertions", "steps"):
                value = parsed.get(key)
                if isinstance(value, list):
                    bullets += [str(v) for v in value]
        if not bullets:
            # 2) Otherwise take markdown-ish bullet lines.
            bullets = [l.lstrip("-*0123456789. ").strip()
                       for l in raw.splitlines()
                       if l.strip().startswith(("-", "*"))
                       or (l.strip()[:2].rstrip(".").isdigit()
                           if l.strip()[:1].isdigit() else False)]
        if not bullets:
            first = next((l.strip() for l in raw.splitlines() if l.strip()), "")
            bullets = [first] if first else []
        if title == "scenario" and bullets:
            title = bullets[0][:70]

        caps = state.get("capabilities")
        can_see = bool(caps and caps.can_screenshot)
        for index, text in enumerate(bullets, start=1):
            criteria.append(AcceptanceCriterion(
                id=f"ac{index}",
                statement=text,
                observable_via=["screen_change"] if can_see else [],
                # Without a model to judge what these sentences mean, calling
                # them critical would let a mechanical run emit a real "fail".
                critical=False))

        return ScenarioSpec(
            title=title,
            intent=raw.strip()[:600],
            acceptance_criteria=criteria,
            is_testable=bool(criteria),
            ambiguities=[
                "no LLM was available, so the scenario was NOT interpreted - "
                "criteria were lifted verbatim from the text and marked "
                "non-critical. Any verdict from this run is mechanical only."],
            estimated_steps=max(1, len(criteria)),
        )
