"""
executor.py - AGENT 4: perform the testing.

Executes ONE step per graph tick (the graph loops back), so LangGraph can route
after every step - which is what makes `adaptive` mode possible without a nested
control flow hidden inside this file.

Each step is: dispatch -> GLANCE -> settle -> observe -> judge.

WHY THERE ARE NOW TWO LOOKS PER STEP
------------------------------------
The old cycle was dispatch -> settle -> observe, one screenshot, one moment. Run
20260817-105323 shows why one moment is not enough:

    step_07 macro nav_test   screen changed 3.257%   <- the input DID arrive
    step_09 observe          screen changed 0.074%   <- FAIL

Both statements are true. xCloud's highlight animated in response to the D-pad
and then settled, and the observation that decided the verdict was taken after
it had settled. The harness photographed its own evidence disappearing and then
reported that there had never been any. The RCA agent diagnosed this itself, as
`harness_defect`, at 85% confidence.

So each acting step is now observed TWICE, via `ctx.timing`:

    GLANCE   ~0.45s after the input - catches the transient reaction
    SETTLE   after the animation completes - the stable, judgeable state

and `reacted_on` records which look moved. A step is credited with a reaction if
EITHER did, because the claim under test is "the input reached xCloud" and a
transient highlight proves that exactly as well as a permanent one.

This also tightens `silent_failure` rather than loosening it. It now requires
BOTH looks to be still, so it means what it says - the firmware queued a report
and nothing whatsoever happened - instead of sometimes meaning "we looked late".


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
from pathlib import Path

from pydantic import BaseModel


from ..logbook import log
from ..schemas import (ActionKind, Observation, PlanStep, StepResult, TestPlan,
                       Verdict)
from ..state import GraphState
from .base import Agent

# Action kinds that send something to the phone and should therefore move the
# screen. Named once, because three separate places used to re-list them and a
# kind added to one list but not another would silently stop being checked.
INPUT_KINDS = (ActionKind.PRESS, ActionKind.HOLD, ActionKind.MACRO,
               ActionKind.STICK, ActionKind.TRIGGER, ActionKind.SPECIAL,
               ActionKind.LAUNCH_PWA, ActionKind.ADB_TEXT, ActionKind.ADB_KEYEVENT)


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
* You may be given TWO frames for one step: a GLANCE taken moments after the
  input, and a SETTLED frame taken after any animation finished. Read both.
  A cloud UI animates, so a real reaction can appear in the glance and be gone
  from the settled frame. If EITHER frame moved, the input reached the app -
  a transient highlight is proof of arrival, not the absence of one. Only
  "neither frame moved" is evidence that nothing happened.
* `screen_changed=false` in BOTH frames after an input that should have moved
  something is strong evidence for FALSE. Say so. `screen_changed=false` in the
  settled frame ALONE is not: it may only mean the UI finished animating.
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
        waited_before = self.ctx.timing.total_waited
        result = StepResult(step=step)

        # The settle profile is resolved BEFORE acting, so the log can announce
        # what the step intends to wait for rather than reporting it afterwards.
        profile = self.ctx.timing.profile_for(step.kind)
        result.settle_profile = profile.describe()

        log.step(f"[{cursor + 1}/{len(plan.steps)}] {step.id}  "
                 f"{step.kind.value} {step.target or ''}".rstrip())
        if step.intent:
            log.act(f"intent: {step.intent}")
        if step.expectation:
            log.act(f"expects: {step.expectation}")
        # NOTE: the D-pad `times` cap that used to live here has been REMOVED.
        #
        # It rewrote `times=3` to `times=1` for directional presses, and it was
        # needed only because this path makes the planner guess a repeat count
        # before the first screenshot exists. Capping the count did not fix that
        # - it just stopped the guess being three times as wrong, and it left no
        # mechanism to issue the second press if one was not enough.
        #
        # The real fix is `execution.mode: closed_loop`, where the action is
        # chosen AFTER each observation and `Action.times` is constrained to 1 by
        # the schema, so a repeat count is unrepresentable rather than corrected.


        # -- 1: act --------------------------------------------------------
        if self.s.get("logs.logcat_enabled", True) and self.ctx.android:
            # Clear first so the excerpt afterwards is about THIS step only.
            self.ctx.android.clear_logcat()

        result.dispatched, result.hardware_ok, detail = self._dispatch(step)
        if detail:
            result.reasoning = detail
        (log.ok if result.hardware_ok else log.warn)(
            f"dispatched {step.kind.value} {step.target or ''} -> "
            f"hardware_ok={result.hardware_ok}"
            + (f" | {detail.splitlines()[0][:110]}" if detail else ""),
            indent=1)

        # -- 2: GLANCE - the wait that catches a TRANSIENT reaction ---------
        # This is the whole point of the change. A press is followed by a short
        # wait and an IMMEDIATE look, because xCloud's highlight animates and
        # then settles: whatever moved is gone by the time the settled frame is
        # taken. Run 20260817-105323 lost its evidence exactly here.
        glance: Observation | None = None
        if self._should_glance(step):
            self.ctx.timing.glance(profile)
            glance = self._look(state, step, profile, phase="glance")
            result.glance_observation = glance

        # -- 3: settle, then the second look -------------------------------
        observation: Observation | None = None
        if step.observe_after:
            if step.kind != ActionKind.WAIT:
                self.ctx.timing.settle(profile)
            observation = self._look(state, step, profile, phase="settle")
            observation.pad_state = self.ctx.pad.state()
            result.observation = observation

        # Which look, if either, saw the screen move. Decided here rather than
        # inside _judge so it is a plain fact in the report that a human can
        # check against the two screenshots.
        result.reacted_on = self._reaction_phase(glance, observation)
        result.waited_seconds = round(
            self.ctx.timing.total_waited - waited_before, 3)

        # -- 4: judge ------------------------------------------------------
        self._judge(result, observation, glance)
        result.duration_seconds = round(time.time() - started, 3)

        verdict_word = {True: "MET", False: "NOT MET",
                        None: "UNKNOWN"}[result.expectation_met]
        log.judge(f"{verdict_word} (confidence {result.confidence:.0%}, "
                  f"reacted_on={result.reacted_on}) - "
                  f"{result.reasoning[:160]}")
        if result.silent_failure:
            log.error(f"SILENT FAILURE at {step.id}: the firmware accepted the "
                      f"command and NEITHER the glance nor the settled frame "
                      f"moved", indent=1)
        log.act(f"step took {result.duration_seconds:.1f}s, of which "
                f"{result.waited_seconds:.1f}s was deliberate waiting")

        # -- 5: route ------------------------------------------------------
        mode = str(self.s.get("execution.mode", "adaptive")).lower()
        failed = (result.expectation_met is False
                  or (not result.hardware_ok and not step.optional))
        needs_rca = bool(failed and not step.optional and mode != "plan")

        next_cursor = cursor + 1
        adaptations: list[str] = []
        if failed and step.optional:
            # An optional step failing is information, not a fault: the wake-up
            # press is expected to have no visible effect.
            adaptations.append(
                f"{step.id} was optional and did not meet its expectation; "
                f"continuing as designed")
        if result.reacted_on == "glance":
            # Worth recording loudly: it means the ONLY proof that input arrived
            # was the transient frame. Under the pre-fix single-look harness this
            # step would have been reported as a failure.
            note = (f"{step.id} reacted only in the GLANCE frame "
                    f"({profile.glance:.2f}s after the input) and had settled "
                    f"back by {profile.total:.2f}s. The input DID reach xCloud; "
                    f"a single late observation would have missed it.")
            adaptations.append(note)
            log.warn(note, indent=1)

        # NOTE: the cursor-jump block that used to live here has been REMOVED.
        #
        # It read `observation.detail_page_open`, then scanned FUTURE plan steps
        # and string-matched their `intent` PROSE for "play"/"launch"/"stream" to
        # guess which step to skip ahead to. That is a state machine emulated
        # with a substring search over English, and it existed only because a
        # pre-written linear plan cannot branch.
        #
        # It also depended on the game-specific flags that `tools/vision.py` used
        # to set from `if "minecraft" in combined`, which were unreliable in both
        # directions and have been removed as well.
        #
        # In `execution.mode: closed_loop` the branch is real: the observed
        # GameState decides the next action, so arriving at a detail page simply
        # produces a different decision on the next pass. Nothing needs to guess
        # where to jump, because there is no list to jump within.

        if needs_rca:

            log.warn(f"routing to RCA after {step.id}", indent=1)

        return {
            "step_results": [result],
            "cursor": next_cursor,
            "needs_rca": needs_rca,
            "adaptations": adaptations,
            "agent_trace": [self.trace(
                "execute",
                f"{step.id} {step.kind.value} {step.target or ''} -> "
                f"hardware_ok={result.hardware_ok} "
                f"expectation_met={result.expectation_met} "
                f"reacted_on={result.reacted_on} "
                f"waited={result.waited_seconds}s "
                f"silent_failure={result.silent_failure}",
                step_id=step.id)],
        }

    # -- the two looks -----------------------------------------------------
    def _should_glance(self, step: PlanStep) -> bool:
        """Glance only where a transient reaction is possible and wanted.

        Skipped for WAIT (nothing was sent) and for OBSERVE/ASSERT (they ARE the
        observation, so a second screenshot would only cost time and an API
        call). Also skippable wholesale via config for a run on a metered key -
        it doubles the number of screenshots and vision calls.
        """
        if not step.observe_after:
            return False
        if step.kind not in INPUT_KINDS:
            return False
        if not self.s.get("execution.settle.glance_enabled", True):
            return False
        return True

    def _look(self, state: GraphState, step: PlanStep, profile: object,
              phase: str) -> Observation:
        """Take ONE observation and record its artefact.

        `phase` reaches the label (and so the screenshot filename), which is what
        makes the pair reviewable side by side afterwards: `step_07_nav_test
        .glance.png` next to `step_07_nav_test.settle.png` is the evidence that
        the old harness threw away.
        """
        when = ("immediately after" if phase == "glance"
                else "after the UI was given time to settle following")
        question = (
            f"A test step just ran: {step.kind.value} "
            f"{step.target or ''} (intent: {step.intent or 'not stated'}).\n"
            f"This screenshot was taken {when} that step "
            f"({'a transient reaction may still be mid-animation' if phase == 'glance' else 'any animation should have finished'}).\n"
            f"Expected afterwards: {step.expectation or 'nothing specific'}.\n"
            f"Describe what is actually on screen now, and say plainly "
            f"whether that expectation appears to hold.")

        label = step.id if phase == "settle" else f"{step.id}.{phase}"
        obs = self.ctx.vision.observe(
            run_id=state["run_id"], label=label, question=question,
            previous_frame=self.ctx.last_frame_path)

        if obs.screenshot_path:
            # Advance the diff baseline on BOTH looks. The settled frame must be
            # compared against the glance, not against the pre-input frame, or a
            # transient change would be counted twice and a settled screen would
            # look like it had moved.
            self.ctx.last_frame_path = obs.screenshot_path
            self.ctx.artifacts.append(obs.screenshot_path)

        shot = Path(obs.screenshot_path).name if obs.screenshot_path else "NONE"
        log.see(f"{phase}: changed={obs.screen_changed} "
                f"ratio={self._ratio(obs)} "
                f"sensors={','.join(obs.sensors_used) or 'NONE'} shot={shot}")

        if obs.screen_description:
            log.see(f"{phase} vision: {obs.screen_description[:200]}", indent=2)
        for note in obs.notes:
            log.debug(f"{phase} note: {note}", indent=2)
        return obs

    @staticmethod
    def _reaction_phase(glance: Observation | None,
                        settled: Observation | None) -> str:
        """Which look saw motion. 'unknown' when no frame diff was possible."""
        g = glance.screen_changed if glance is not None else None
        s = settled.screen_changed if settled is not None else None
        if g is None and s is None:
            return "unknown"
        if g is True and s is True:
            return "both"
        if g is True:
            return "glance"
        if s is True:
            return "settle"
        return "neither"


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
                # needs real time before anything can be judged. Routed through
                # ctx.timing like every other wait, so it shows up in the log and
                # in the "where did the time go" total rather than being an
                # invisible six-second gap in the transcript.
                self.ctx.timing.sleep(
                    float(self.s.get("android.pwa.settle_seconds", 6.0)),
                    "android.pwa.settle_seconds - a PWA is a page load over the "
                    "network, so nothing can be judged before it arrives")
            return True, ok, detail

        if step.kind == ActionKind.ADB_TEXT:
            if not self.ctx.android or not self.ctx.android.status.adb_available:
                return False, False, ("cannot send text via ADB: adb is not available.")
            text = str(step.target or "")
            ok, detail = self.ctx.android.input_text(text)
            return True, ok, detail

        if step.kind == ActionKind.ADB_KEYEVENT:
            if not self.ctx.android or not self.ctx.android.status.adb_available:
                return False, False, ("cannot send keyevent via ADB: adb is not available.")
            key = str(step.target or "66")
            ok, detail = self.ctx.android.keyevent(key)
            return True, ok, detail

        if step.kind == ActionKind.WAIT:
            seconds = float(step.seconds or step.duration or 1.0)
            self.ctx.timing.sleep(
                seconds, f"explicit WAIT step {step.id}: "
                         f"{step.intent or 'no reason given'}")
            return True, True, f"waited {seconds:.1f}s"

        ok, detail = self.ctx.pad.dispatch(step)
        return True, ok, detail


    # -- judgement ---------------------------------------------------------
    def _judge(self, result: StepResult, obs: Observation | None,
               glance: Observation | None = None) -> None:
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

        # Did EITHER look see the screen move? This is the question that decides
        # a silent failure, and it must be asked of both frames.
        #
        # `reacted_on` was already computed in `run`, so the two can never
        # disagree - a judgement citing "neither frame moved" while the report
        # shows reacted_on=glance would be exactly the kind of internal
        # contradiction that makes a test report untrustworthy.
        moved_somewhere = result.reacted_on in ("glance", "settle", "both")
        nothing_moved = result.reacted_on == "neither"

        # The mechanical check first: it costs nothing and it is the one that
        # catches the silent failure.
        #
        # NOT in dry-run. There, `hardware_ok` means "the command was printed",
        # no bytes ever left the PC, and an unchanged screen is the CORRECT
        # outcome. Flagging it would manufacture a hardware fault out of a mode
        # whose whole purpose is to touch no hardware.
        #
        # NOTE the bar is now `nothing_moved` (BOTH frames still), not
        # `obs.screen_changed is False` (the settled frame alone). That single
        # change is what stops the harness reporting run 20260817-105323's
        # nav_test as a hardware fault when the pixels had in fact moved 3.257%
        # a moment earlier.
        if (nothing_moved
                and not self.s.get("hardware.dry_run", False)
                and step.kind in INPUT_KINDS):
            result.silent_failure = bool(result.hardware_ok)

        judgement = self.think(_Judgement, self.system_prompt(JUDGE_ROLE),
                               self._evidence(result, obs, glance), default=None)


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
            #
            # It now reads BOTH frames. Asking only the settled one is what
            # produced the false negative this whole change exists to fix.
            if nothing_moved:
                result.expectation_met = False
                result.confidence = 0.6
                result.reasoning = (
                    f"no LLM available. Mechanically: neither the glance "
                    f"({self._ratio(glance)}) nor the settled frame "
                    f"({self._ratio(obs)}) moved past the motion threshold, so "
                    f"the UI did not react to this step at either moment.")
            elif moved_somewhere:
                result.expectation_met = None
                result.confidence = 0.3
                result.reasoning = (
                    f"no LLM available. The screen DID change "
                    f"(glance {self._ratio(glance)}, settled "
                    f"{self._ratio(obs)}; reacted_on={result.reacted_on}), so "
                    f"something reacted, but whether it matched the expectation "
                    f"cannot be determined mechanically.")
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
                " OVERRIDDEN: the firmware accepted the command but NEITHER the "
                "glance nor the settled frame changed, which is a silent failure "
                "- the report was queued and the app did not react.")
        elif (result.expectation_met is False and result.reacted_on == "glance"
              and not step.optional):
            # The inverse guard, and the one that matters for this bug class. If
            # the model judged FALSE from the settled frame while the glance
            # clearly moved, the judgement is about the WRONG MOMENT. Downgrade
            # to inconclusive rather than recording a failure we can see is
            # contradicted by our own evidence - "we looked too late" is not the
            # same finding as "the input did not arrive", and conflating them is
            # what sent run 20260817-105323's reader after a USB-OTG problem
            # that did not exist.
            result.expectation_met = None
            result.confidence = min(result.confidence, 0.4)
            result.reasoning += (
                f" DOWNGRADED to inconclusive: the settled frame supports this "
                f"FALSE verdict, but the glance taken {(glance.change_ratio or 0):.2%} "
                f"earlier DID move. The input reached xCloud and the reaction had "
                f"settled by the time the judged frame was taken, so this is a "
                f"question of WHEN we looked, not of whether the app responded.")

    @staticmethod
    def _ratio(obs: Observation | None) -> str:
        """Render a change ratio for prose, including 'not measured'."""
        if obs is None:
            return "not taken"
        if obs.change_ratio is None:
            return "not measured"
        return f"{obs.change_ratio:.2%}"

    @classmethod
    def _evidence(cls, result: StepResult, obs: Observation,
                  glance: Observation | None = None) -> str:
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
            f"  waits applied: {result.settle_profile}",
            "",
        ]

        # The glance goes FIRST, deliberately. It is the frame most likely to
        # contain the proof, and a model reading the settled frame first tends to
        # anchor on "nothing is happening" and then discount the earlier one.
        if glance is not None:
            lines += [
                "EVIDENCE 1 of 2 - THE GLANCE, taken moments after the input, "
                "while a reaction may still be mid-animation",
                f"  screen changed: {glance.screen_changed} "
                f"(ratio {glance.change_ratio})",
                f"  sensors used: {', '.join(glance.sensors_used) or 'NONE'}",
            ]
            if glance.screen_description:
                lines += ["  screen description:",
                          f"    {glance.screen_description}"]
            if glance.screen_text:
                lines += ["  on-screen text (OCR):",
                          f"    {glance.screen_text[:600]}"]
            lines.append("")

        lines += [
            ("EVIDENCE 2 of 2 - THE SETTLED FRAME, taken after the UI was given "
             "time to finish animating" if glance is not None
             else "EVIDENCE GATHERED AFTER THE STEP"),
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

        if glance is not None:
            lines += [
                "",
                "HOW TO WEIGH THE TWO FRAMES",
                f"  reacted_on = {result.reacted_on}",
                "  A cloud UI animates: a selection highlight can move and "
                "settle within a second. If the GLANCE moved and the settled "
                "frame did not, the input DID reach xCloud - a transient "
                "reaction is proof of arrival, not an absence of one. Only "
                "'neither frame moved' is evidence that nothing happened.",
            ]
        return "\n".join(lines)


