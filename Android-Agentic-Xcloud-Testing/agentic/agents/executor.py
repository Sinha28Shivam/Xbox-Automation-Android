"""
executor.py - AGENT 4: perform the testing.

Executes ONE step per graph tick (the graph loops back), so LangGraph can route
after every step - which is what makes `adaptive` mode possible without a nested
control flow hidden inside this file.

Each step is: dispatch -> settle -> observe -> judge.

THE JUDGEMENT IS THE POINT
--------------------------
`hardware_ok` and `expectation_met` are kept strictly separate:

    hardware_ok      the firmware queued the HID report
    expectation_met  the SCREEN shows what the step said it should

Their disagreement is the most valuable signal this system produces. hardware_ok
with expectation_met=False and no screen change is a SILENT FAILURE - the parent
README's "Commands say ok but phone does nothing". A rig that only reported
`hardware_ok` would call that a pass, which is exactly the failure mode this
whole layer exists to eliminate.

`expectation_met=None` is a first-class outcome and means "we could not tell".
It is not a pass. Anywhere it appears, the verdict is capped at inconclusive.
"""

from __future__ import annotations

import time

from pydantic import BaseModel

from ..schemas import (ActionKind, Observation, PlanStep, StepResult, TestPlan,
                       Verdict)
from ..state import GraphState
from .base import Agent

JUDGE_ROLE = """\
You judge whether ONE test step achieved what it was supposed to.

You are given the step's expectation and the evidence gathered right after it.
Reply with:
  expectation_met : true / false / null   (null = the evidence cannot settle it)
  confidence      : 0.0 - 1.0
  reasoning       : one or two sentences citing the EVIDENCE, not intuition

Judging rules that matter more than being agreeable:
* "The command was accepted" is NOT evidence that the UI reacted. If the only
  evidence is a firmware OK, the answer is null, not true.
* `screen_changed=false` after an input that should have moved something is
  strong evidence for FALSE. Say so.
* A cloud stream is video: a small change ratio can be nothing but compression
  noise, while a real navigation usually changes a lot. Weigh magnitude.
* Browser UI (address bar, tabs) in the screenshot is expected - xCloud is a PWA.
  It is not a failure by itself.
* A visible dialog, app chooser or on-screen keyboard WOULD steal gamepad input.
  If you see one, that is a finding worth stating.
* When the sensors were degraded or absent, prefer null over a guess.
"""


class _Judgement(BaseModel):
    """Local schema: only this agent ever needs it, so it is not in schemas.py."""
    expectation_met: bool | None = None
    confidence: float = 0.0
    reasoning: str = ""


class ExecutorAgent(Agent):
    name = "executor"

    def run(self, state: GraphState) -> GraphState:
        plan: TestPlan | None = state.get("plan")
        cursor = int(state.get("cursor", 0))
        if plan is None or cursor >= len(plan.steps):
            return {"agent_trace": [self.trace("execute", "nothing left to do")]}

        # Budget guard. A hung stream must not burn the whole window silently.
        if self.ctx.out_of_time():
            return {
                "halt_reason": (
                    f"run exceeded safety.max_run_seconds "
                    f"({self.s.get('safety.max_run_seconds')}s) at step "
                    f"{cursor + 1}/{len(plan.steps)}"),
                "agent_trace": [self.trace("execute", "time budget exhausted")],
            }

        step = plan.steps[cursor]
        started = time.time()
        result = StepResult(step=step)

        # -- 1: act --------------------------------------------------------
        if self.s.get("logs.logcat_enabled", True) and self.ctx.android:
            # Clear first so the excerpt afterwards is about THIS step only.
            self.ctx.android.clear_logcat()

        result.dispatched, result.hardware_ok, detail = self._dispatch(step)
        if detail:
            result.reasoning = detail

        # -- 2: settle -----------------------------------------------------
        # The UI animates and the network adds 60-100ms; observing immediately
        # photographs the BEFORE state and manufactures a false failure.
        if step.observe_after and step.kind != ActionKind.WAIT:
            time.sleep(float(self.s.get("execution.observe_delay_seconds", 1.5)))

        # -- 3: observe ----------------------------------------------------
        observation: Observation | None = None
        if step.observe_after:
            question = (
                f"A test step just ran: {step.kind.value} "
                f"{step.target or ''} (intent: {step.intent or 'not stated'}).\n"
                f"Expected afterwards: {step.expectation or 'nothing specific'}.\n"
                f"Describe what is actually on screen now, and say plainly "
                f"whether that expectation appears to hold.")
            observation = self.ctx.vision.observe(
                run_id=state["run_id"], label=step.id, question=question,
                previous_frame=self.ctx.last_frame_path)
            observation.pad_state = self.ctx.pad.state()
            if observation.screenshot_path:
                self.ctx.last_frame_path = observation.screenshot_path
                self.ctx.artifacts.append(observation.screenshot_path)
            result.observation = observation

        # -- 4: judge ------------------------------------------------------
        self._judge(result, observation)
        result.duration_seconds = round(time.time() - started, 3)

        # -- 5: route ------------------------------------------------------
        mode = str(self.s.get("execution.mode", "adaptive")).lower()
        failed = (result.expectation_met is False
                  or (not result.hardware_ok and not step.optional))
        needs_rca = bool(failed and not step.optional and mode != "plan")

        adaptations: list[str] = []
        if failed and step.optional:
            # An optional step failing is information, not a fault: the wake-up
            # press is expected to have no visible effect.
            adaptations.append(
                f"{step.id} was optional and did not meet its expectation; "
                f"continuing as designed")

        return {
            "step_results": [result],
            "cursor": cursor + 1,
            "needs_rca": needs_rca,
            "adaptations": adaptations,
            "agent_trace": [self.trace(
                "execute",
                f"{step.id} {step.kind.value} {step.target or ''} -> "
                f"hardware_ok={result.hardware_ok} "
                f"expectation_met={result.expectation_met} "
                f"silent_failure={result.silent_failure}",
                step_id=step.id)],
        }

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, step: PlanStep) -> tuple[bool, bool, str]:
        """Route a step to the right tool. Returns (dispatched, ok, detail)."""
        if step.kind == ActionKind.OBSERVE:
            # Nothing to send; the observation IS the step.
            return True, True, "observation checkpoint, no input sent"

        if step.kind == ActionKind.ASSERT:
            # An assertion is evaluated from the observation, not dispatched.
            return True, True, "assertion evaluated from the observation"

        if step.kind == ActionKind.LAUNCH_PWA:
            if not self.ctx.android or not self.ctx.android.status.adb_available:
                return False, False, ("cannot launch the PWA: adb is not "
                                      "available. Open xbox.com/play on the "
                                      "phone by hand and re-run.")
            ok, detail = self.ctx.android.launch_pwa(step.target)
            if ok:
                # A PWA is a page load over the network, not an app start: it
                # needs real time before anything can be judged.
                time.sleep(float(self.s.get("android.pwa.settle_seconds", 6.0)))
            return True, ok, detail

        if step.kind == ActionKind.WAIT:
            seconds = float(step.seconds or step.duration or 1.0)
            time.sleep(seconds)
            return True, True, f"waited {seconds:.1f}s"

        ok, detail = self.ctx.pad.dispatch(step)
        return True, ok, detail

    # -- judgement ---------------------------------------------------------
    def _judge(self, result: StepResult, obs: Observation | None) -> None:
        step = result.step

        # No expectation was declared, so there is nothing to check. Honest, but
        # it contributes no evidence - which the evaluator accounts for.
        if not step.expectation:
            result.expectation_met = None
            result.confidence = 0.0
            result.reasoning = (result.reasoning + " " if result.reasoning else "") + \
                "no expectation was declared for this step, so nothing was checked"
            return

        if not result.hardware_ok and not step.optional:
            result.expectation_met = False
            result.confidence = 0.9
            result.reasoning = (
                f"the command itself failed, so the expectation cannot hold. "
                f"{result.reasoning}")
            return

        if obs is None or not obs.sensors_used:
            result.expectation_met = None
            result.confidence = 0.0
            result.reasoning += (
                " no sensors were available, so whether the app reacted is "
                "unknown - a firmware OK does not answer it")
            return

        # The mechanical check first: it costs nothing and it is the one that
        # catches the silent failure.
        #
        # NOT in dry-run. There, `hardware_ok` means "the command was printed",
        # no bytes ever left the PC, and an unchanged screen is the CORRECT
        # outcome. Flagging it would manufacture a hardware fault out of a mode
        # whose whole purpose is to touch no hardware.
        if (obs.screen_changed is False
                and not self.s.get("hardware.dry_run", False)
                and step.kind in (
                    ActionKind.PRESS, ActionKind.HOLD, ActionKind.MACRO,
                    ActionKind.STICK, ActionKind.TRIGGER, ActionKind.SPECIAL,
                    ActionKind.LAUNCH_PWA)):
            result.silent_failure = bool(result.hardware_ok)

        judgement = self.think(_Judgement, self.system_prompt(JUDGE_ROLE),
                               self._evidence(result, obs), default=None)

        if judgement is not None:
            result.expectation_met = judgement.expectation_met
            result.confidence = judgement.confidence
            result.reasoning = judgement.reasoning
        elif self.s.get("hardware.dry_run", False):
            # Dry run: nothing was sent, so no observation can bear on the
            # expectation. Saying "unknown" is the only honest answer.
            result.expectation_met = None
            result.confidence = 0.0
            result.reasoning = ("DRY RUN: the command was printed, not sent, so "
                                "nothing can be concluded about the device")
        else:
            # Mechanical fallback: the frame diff is the only thing we can read
            # without a model. It can say "nothing happened" - which is the
            # verdict that matters - but it cannot confirm the RIGHT thing
            # happened, so a change yields None rather than True.
            if obs.screen_changed is False:
                result.expectation_met = False
                result.confidence = 0.6
                result.reasoning = (
                    f"no LLM available. Mechanically: the screen changed by "
                    f"{(obs.change_ratio or 0):.2%}, below the motion "
                    f"threshold, so the UI did not react to this step.")
            elif obs.screen_changed is True:
                result.expectation_met = None
                result.confidence = 0.3
                result.reasoning = (
                    f"no LLM available. The screen DID change "
                    f"({(obs.change_ratio or 0):.2%}), so something reacted, but "
                    f"whether it matched the expectation cannot be determined "
                    f"mechanically.")
            else:
                result.expectation_met = None
                result.confidence = 0.0
                result.reasoning = ("no LLM and no frame comparison available; "
                                    "this step is unjudged")

        if result.silent_failure and result.expectation_met is not False:
            # Trust the pixels over the prose. hardware_ok + an unchanged screen
            # is the documented trap, and no amount of model optimism outranks it.
            result.expectation_met = False
            result.reasoning += (
                " OVERRIDDEN: the firmware accepted the command but the screen "
                "did not change, which is a silent failure - the report was "
                "queued and the app did not react.")

    @staticmethod
    def _evidence(result: StepResult, obs: Observation) -> str:
        step = result.step
        lines = [
            "STEP",
            f"  kind: {step.kind.value}   target: {step.target}",
            f"  times={step.times} duration={step.duration} "
            f"interval={step.interval} value={step.value} "
            f"direction={step.direction}",
            f"  intent: {step.intent}",
            f"  EXPECTATION: {step.expectation}",
            f"  firmware accepted the command (hardware_ok): {result.hardware_ok}",
            "",
            "EVIDENCE GATHERED AFTER THE STEP",
            f"  sensors used: {', '.join(obs.sensors_used) or 'NONE'}",
            f"  degraded: {obs.degraded}",
            f"  screen changed: {obs.screen_changed} "
            f"(ratio {obs.change_ratio})",
            f"  focused window: {obs.focused_window}",
            f"  board reports it is holding: {obs.pad_state}",
        ]
        if obs.screen_description:
            lines += ["  screen description:", f"    {obs.screen_description}"]
        if obs.screen_text:
            lines += ["  on-screen text (OCR, may be noisy):",
                      f"    {obs.screen_text[:1200]}"]
        if obs.log_excerpt:
            lines += ["  relevant log lines:",
                      "\n".join(f"    {l}" for l in
                                obs.log_excerpt.splitlines()[:25])]
        if obs.notes:
            lines += ["  notes: " + " | ".join(obs.notes)]
        if result.reasoning:
            lines += [f"  dispatch output: {result.reasoning[:500]}"]
        return "\n".join(lines)
