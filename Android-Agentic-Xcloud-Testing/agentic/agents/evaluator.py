"""
evaluator.py - AGENT 5: decide the verdict against the acceptance criteria.

Kept separate from the executor on purpose. The executor judges STEPS ("did the
tile move?"); the evaluator judges the SCENARIO ("can the user reach their
library with a controller?"). Those are different questions, and merging them
lets a run with every step green claim a pass for a criterion nothing ever
actually checked.

THE VERDICT CEILING
-------------------
`_ceiling` is enforced in code, after the model has spoken, and can only lower a
verdict:

    no sensors at all       -> INCONCLUSIVE. Nothing was observed, so nothing
                              can be claimed.
    dry run                 -> INCONCLUSIVE. No bytes reached the phone.
    a criterion unverified  -> at best INCONCLUSIVE, never PASS.
    a silent failure seen   -> FAIL, whatever else looks fine.

This is the safeguard against the single most damaging output of an agentic test
system: a confident PASS that nobody checked. A model asked "did it work?" with
thin evidence will tend to be agreeable; the ceiling makes agreeableness
insufficient.
"""

from __future__ import annotations

from ..schemas import (CriterionResult, Evaluation, ScenarioSpec, StepResult,
                       Verdict)
from ..state import GraphState
from .base import Agent

ROLE = """\
You decide, criterion by criterion, whether the scenario passed.

For each acceptance criterion return: met (true/false/null), confidence,
the evidence you relied on, and short reasoning.

Then set an overall verdict:
  pass          every CRITICAL criterion is met on real evidence
  fail          at least one critical criterion is demonstrably not met
  inconclusive  the run did not produce enough evidence to say
  blocked       the test could not meaningfully start
  error         the harness itself malfunctioned

Rules you must not bend:
* met=null is correct and expected when evidence is missing. Never turn null
  into true because the steps "seemed fine" or because the hardware said OK.
* A step succeeding is not the same as a criterion being met. Ask specifically
  what evidence supports THIS criterion.
* If a step shows a silent failure (the command was accepted but the screen did
  not change), that is strong evidence AGAINST any criterion depending on it.
* List every reason the verdict might be wrong in `caveats`. A caveat costs
  nothing; a false pass costs a real bug shipped.
"""


class EvaluatorAgent(Agent):
    name = "evaluator"

    def run(self, state: GraphState) -> GraphState:
        scenario: ScenarioSpec | None = state.get("scenario")
        results: list[StepResult] = list(state.get("step_results", []))

        if scenario is None:
            evaluation = Evaluation(
                verdict=Verdict.ERROR,
                summary="no scenario specification reached the evaluator",
                caveats=["this is a harness fault, not a device finding"])
            return {"evaluation": evaluation, "verdict": Verdict.ERROR,
                    "agent_trace": [self.trace("evaluate", "no scenario")]}

        # A halt before any step ran is BLOCKED, not FAIL. The distinction is
        # what stops a wiring fault being filed as an xCloud bug.
        if not results:
            evaluation = Evaluation(
                verdict=Verdict.BLOCKED,
                summary=("no test step ran, so nothing about xCloud was "
                         "exercised: " + (state.get("halt_reason")
                                          or "reason not recorded")),
                criteria=[CriterionResult(
                    criterion_id=c.id, statement=c.statement, met=None,
                    reasoning="no step ran that could produce evidence")
                    for c in scenario.acceptance_criteria],
                caveats=["the run was blocked before execution"])
            return {"evaluation": evaluation, "verdict": Verdict.BLOCKED,
                    "agent_trace": [self.trace("evaluate", "no steps ran")]}

        evaluation: Evaluation | None = self.think(
            Evaluation, self.system_prompt(ROLE),
            self._evidence(state, scenario, results), default=None)

        if evaluation is None:
            evaluation = self._mechanical_evaluation(scenario, results)

        self._fill_missing_criteria(evaluation, scenario)
        ceiling, reasons = self._ceiling(state, results)
        if self._rank(evaluation.verdict) > self._rank(ceiling):
            evaluation.caveats.append(
                f"verdict lowered from '{evaluation.verdict.value}' to "
                f"'{ceiling.value}' by the evidence rules: " + "; ".join(reasons))
            evaluation.verdict = ceiling
        evaluation.caveats.extend(
            r for r in reasons if r not in evaluation.caveats)

        return {
            "evaluation": evaluation,
            "verdict": evaluation.verdict,
            "agent_trace": [self.trace(
                "evaluate",
                f"verdict={evaluation.verdict.value} "
                f"criteria={len(evaluation.criteria)} "
                f"confidence={evaluation.confidence:.2f}")],
        }

    # -- evidence ----------------------------------------------------------
    def _evidence(self, state: GraphState, scenario: ScenarioSpec,
                  results: list[StepResult]) -> str:
        lines = ["SCENARIO", f"  {scenario.title}", f"  intent: {scenario.intent}",
                 "", "ACCEPTANCE CRITERIA"]
        for crit in scenario.acceptance_criteria:
            lines.append(
                f"  [{crit.id}] {'CRITICAL' if crit.critical else 'non-critical'}"
                f" {crit.statement} (checkable via: "
                f"{', '.join(crit.observable_via) or 'unclear'})")

        plan = state.get("plan")
        if plan is not None:
            lines += ["", f"PLAN STRATEGY (revision {plan.revision})",
                      f"  {plan.strategy}"]
            if plan.assumptions:
                lines.append("  assumptions: " + "; ".join(plan.assumptions))

        lines += ["", "WHAT ACTUALLY HAPPENED"]
        for result in results:
            step = result.step
            lines.append(
                f"  [{step.id}] {step.kind.value} {step.target or ''} "
                f"(criteria: {', '.join(step.criterion_ids) or 'none'})"
                f"{' OPTIONAL' if step.optional else ''}")
            lines.append(f"      expectation: {step.expectation or '(none)'}")
            lines.append(
                f"      hardware_ok={result.hardware_ok} "
                f"expectation_met={result.expectation_met} "
                f"confidence={result.confidence:.2f} "
                f"silent_failure={result.silent_failure}")
            if result.reasoning:
                lines.append(f"      judgement: {result.reasoning[:400]}")
            obs = result.observation
            if obs is not None:
                lines.append(
                    f"      sensors={', '.join(obs.sensors_used) or 'NONE'} "
                    f"screen_changed={obs.screen_changed} "
                    f"({obs.change_ratio}) focus={obs.focused_window}")
                if obs.screen_description:
                    lines.append(f"      saw: {obs.screen_description[:300]}")
                elif obs.screen_text:
                    lines.append(f"      text: {obs.screen_text[:300]}")

        env = state.get("environment")
        if env is not None and env.warnings:
            lines += ["", "ENVIRONMENT WARNINGS THAT LIMIT WHAT CAN BE CLAIMED",
                      *(f"  - {w}" for w in env.warnings)]
        if scenario.risk_notes:
            lines += ["", "KNOWN SCENARIO RISKS",
                      *(f"  - {r}" for r in scenario.risk_notes)]
        return "\n".join(lines)

    # -- ceiling -----------------------------------------------------------
    @staticmethod
    def _rank(verdict: Verdict) -> int:
        """Higher = a stronger claim. Only used to prevent OVER-claiming."""
        return {Verdict.ERROR: 0, Verdict.BLOCKED: 1, Verdict.FAIL: 2,
                Verdict.INCONCLUSIVE: 3, Verdict.PASS: 4}.get(verdict, 3)

    def _ceiling(self, state: GraphState,
                 results: list[StepResult]) -> tuple[Verdict, list[str]]:
        reasons: list[str] = []
        ceiling = Verdict.PASS
        env = state.get("environment")

        if any(r.silent_failure for r in results):
            offenders = [r.step.id for r in results if r.silent_failure]
            reasons.append(
                f"silent failure at {', '.join(offenders)}: the firmware "
                f"accepted the command and the screen did not change, so input "
                f"is not reaching xCloud")
            # A silent failure is a real, demonstrated defect in the path under
            # test. FAIL, not inconclusive.
            return Verdict.FAIL, reasons

        if env is not None and env.pad.dry_run:
            reasons.append(
                "DRY RUN: no command was actually sent to the phone, so this "
                "run cannot pass or fail anything about the device")
            ceiling = Verdict.INCONCLUSIVE

        observed = [r for r in results
                    if r.observation and r.observation.sensors_used]
        if not observed:
            reasons.append(
                "no sensor produced data during the whole run, so nothing about "
                "what the phone displayed was actually verified")
            ceiling = Verdict.INCONCLUSIVE

        checked = [r for r in results if r.step.expectation]
        if not checked:
            reasons.append(
                "no step declared an expectation, so nothing was checked - a "
                "plan of unfailable steps cannot demonstrate a pass")
            ceiling = Verdict.INCONCLUSIVE

        unresolved = [r.step.id for r in checked if r.expectation_met is None]
        if unresolved:
            reasons.append(
                f"steps {', '.join(unresolved)} could not be judged from the "
                f"evidence available")
            ceiling = min(ceiling, Verdict.INCONCLUSIVE, key=self._rank)

        return ceiling, reasons

    # -- fallbacks ---------------------------------------------------------
    def _fill_missing_criteria(self, evaluation: Evaluation,
                               scenario: ScenarioSpec) -> None:
        """A criterion the model forgot must appear as UNVERIFIED, not vanish.

        Silently dropping it would make the report look complete while quietly
        narrowing what was tested.
        """
        seen = {c.criterion_id for c in evaluation.criteria}
        for crit in scenario.acceptance_criteria:
            if crit.id not in seen:
                evaluation.criteria.append(CriterionResult(
                    criterion_id=crit.id, statement=crit.statement, met=None,
                    reasoning="the evaluation did not address this criterion, "
                              "so it is reported as unverified"))
                evaluation.caveats.append(
                    f"criterion {crit.id} was not evaluated")

    def _mechanical_evaluation(self, scenario: ScenarioSpec,
                               results: list[StepResult]) -> Evaluation:
        """No-LLM fallback: aggregate per-step judgements by criterion link."""
        criteria: list[CriterionResult] = []
        by_criterion: dict[str, list[StepResult]] = {}
        for result in results:
            for cid in result.step.criterion_ids:
                by_criterion.setdefault(cid, []).append(result)

        for crit in scenario.acceptance_criteria:
            linked = by_criterion.get(crit.id, [])
            judged = [r for r in linked if r.expectation_met is not None]
            if not judged:
                met: bool | None = None
                reasoning = ("no step produced a judgeable result for this "
                             "criterion")
            elif any(r.expectation_met is False for r in judged):
                met = False
                reasoning = ("at least one linked step did not meet its "
                             "expectation: " + ", ".join(
                                 r.step.id for r in judged
                                 if r.expectation_met is False))
            else:
                met = True
                reasoning = ("every linked step met its expectation: "
                             + ", ".join(r.step.id for r in judged))
            criteria.append(CriterionResult(
                criterion_id=crit.id, statement=crit.statement, met=met,
                confidence=0.4 if met is not None else 0.0,
                evidence=[f"{r.step.id}: {r.reasoning[:120]}" for r in linked],
                reasoning=reasoning))

        critical = [c for c in criteria
                    if any(s.id == c.criterion_id and s.critical
                           for s in scenario.acceptance_criteria)]
        if any(c.met is False for c in critical):
            verdict = Verdict.FAIL
        elif critical and all(c.met is True for c in critical):
            verdict = Verdict.PASS
        else:
            verdict = Verdict.INCONCLUSIVE

        return Evaluation(
            verdict=verdict, criteria=criteria, confidence=0.4,
            summary=("Mechanical evaluation: no LLM was available, so criteria "
                     "were scored purely by aggregating the per-step "
                     "expectation results linked to each one."),
            caveats=["no LLM reasoning was applied to this verdict"])
