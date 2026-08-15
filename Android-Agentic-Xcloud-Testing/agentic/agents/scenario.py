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
3. Set is_testable=false when the CORE of the scenario cannot be verified at all
   (for example it depends on audio, on frame-exact timing, on a specific save
   file, or on subjective image quality). Explain why in `ambiguities`. Refusing
   a run is a valid and useful outcome.
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
