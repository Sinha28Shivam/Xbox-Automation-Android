"""
rca.py - AGENT 6: root cause analysis.

Its job is to answer WHICH LAYER broke, because that is what decides who fixes
it. On this rig a single symptom - "the button press did nothing" - has at least
six genuinely different causes:

    wiring            TX/RX not crossed, no common GND, FTDI jumper on 3.3V
    firmware          wrong sketch, BTN_TABLE mismatch
    host_mode         phone not in OTG host mode (Leonardo's ON LED dark)
    hid_enumeration   the pad never enumerated, though the board answers OK
    pwa_not_loaded    the page is not open, or a chooser/dialog has focus
    app_defect        an actual xCloud bug - the only one worth filing

Guessing among these wastes hours. So the agent is required to produce a
`discriminating_test` for its primary hypothesis: a check that could DISPROVE it.
That is not decoration - it is the discipline that cracked the parent project,
where `verify_hid_raw.py` finally broke a deadlock by reading real HID report
bytes, because every earlier check could only say "the firmware accepted the
command" and never "no".

The agent also decides `retryable`, but `_mechanical_retryable` has the final
word in code: a wiring fault does not heal on retry, and three identical
failures are less useful than one clear "go check the ON LED".
"""

from __future__ import annotations

from ..schemas import (CauseClass, Hypothesis, RootCauseAnalysis, StepResult,
                       Verdict)
from ..state import GraphState
from .base import Agent

ROLE = """\
You are a root-cause analyst for a hardware-in-the-loop Android test rig.

Produce a primary hypothesis plus alternatives. For each: the cause class, how
likely it is, the evidence for AND against it, and - most importantly - a
`discriminating_test`: a concrete check that would PROVE THE HYPOTHESIS WRONG if
it is wrong. A hypothesis with no falsifying test is a guess.

Set `layer` to where the fault lives, and give recommendations ordered by
(likelihood x cheapness to check).

THE CAUSAL CHAIN, in order. A break anywhere upstream produces the same symptom
downstream, so always attribute to the FURTHEST UPSTREAM link the evidence
supports:
  1. PC -> FT232RL over USB (a COM port must exist)
  2. FT232RL -> Leonardo over UART (TX->D0, RX->D1 CROSSED, common GND, 5V)
  3. Leonardo firmware accepts the command and mutates its HID report
  4. Phone is in OTG HOST mode and has ENUMERATED the pad
       - this is the step that fails most often and most silently
       - the board's own 'pad connected' bit is the authority here
       - the phone POWERS the board in host mode, so a dark ON LED means no
         host mode and nothing downstream can possibly work
  5. Android routes the HID report to the focused window
  6. The focused window is a BROWSER with the xCloud PWA loaded
       - a permission dialog, app chooser or keyboard would steal the input
       - xCloud shows "connect a controller" until it sees the first input
  7. xCloud's web app reacts, and the change survives the stream round trip

DISTINGUISHING RULES THAT MATTER:
* hardware_ok=true with NO screen change is NOT an app bug by default. Suspect
  steps 4-6 first: they all produce exactly this signature.
* Only call it app_defect when input demonstrably ARRIVED (an earlier step in the
  same run visibly worked) and the app then misbehaved. xCloud is a PWA served
  fresh from the network, so it CAN regress with no local change - but say that
  only when the evidence supports it.
* If an earlier step in the run DID change the screen, steps 1-5 are proven good
  for this session. That is powerful evidence: use it to rule causes out.
* Consider the harness and the scenario as suspects too. An expectation that was
  never observable, or a wait shorter than the stream's latency, is our fault,
  not the app's.
"""


class RootCauseAgent(Agent):
    name = "rca"

    def run(self, state: GraphState) -> GraphState:
        results: list[StepResult] = list(state.get("step_results", []))
        failing = self._failing_step(results)

        analysis: RootCauseAnalysis | None = self.think(
            RootCauseAnalysis, self.system_prompt(ROLE),
            self._evidence(state, results, failing), default=None)

        if analysis is None:
            analysis = self._mechanical_analysis(state, results, failing)

        if failing is not None and not analysis.failure_step_id:
            analysis.failure_step_id = failing.step.id

        # Code decides retryability, not the model: it must be stable across runs
        # and it governs whether we spend another few minutes of hardware time.
        retryable, strategy = self._mechanical_retryable(analysis, state)
        if analysis.retryable and not retryable:
            analysis.recommendations.append(
                f"the analysis suggested a retry, but cause class "
                f"'{analysis.primary.cause_class.value}' is not in "
                f"retry.retryable_causes - retrying would reproduce the same "
                f"failure without adding information")
        analysis.retryable = retryable
        analysis.retry_strategy = strategy or analysis.retry_strategy

        return {
            "root_cause": analysis,
            "needs_rca": False,
            "agent_trace": [self.trace(
                "root_cause",
                f"layer={analysis.layer} "
                f"cause={analysis.primary.cause_class.value} "
                f"likelihood={analysis.primary.likelihood:.2f} "
                f"retryable={analysis.retryable}")],
        }

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _failing_step(results: list[StepResult]) -> StepResult | None:
        """First real failure. Optional steps are excluded - they are allowed
        to fail, so treating one as the fault would misdirect the whole RCA."""
        for result in results:
            if result.step.optional:
                continue
            if result.expectation_met is False or not result.hardware_ok:
                return result
        return None

    def _evidence(self, state: GraphState, results: list[StepResult],
                  failing: StepResult | None) -> str:
        env = state.get("environment")
        scenario = state.get("scenario")
        lines: list[str] = []

        if env is not None:
            pad, android = env.pad, env.android
            lines += [
                "RIG STATE AT THE START OF THE RUN",
                f"  serial link open      : {pad.link_open} on {pad.port}",
                f"  firmware / transport  : {pad.firmware} / {pad.transport}",
                f"  PAD ENUMERATED BY A HOST (phone in OTG host mode): "
                f"{pad.pad_connected_to_phone}",
                f"  dry run               : {pad.dry_run}",
                f"  pad diagnostics       : "
                f"{' | '.join(pad.diagnostics) or 'none'}",
                f"  adb                   : {android.adb_available} "
                f"state={android.device_state}",
                f"  device                : {android.model} "
                f"Android {android.android_version}",
                f"  screen on             : {android.screen_on}",
                f"  focused at start      : {android.focused_window}",
                f"  browsers found        : "
                f"{', '.join(android.browsers_found) or 'none'}",
                f"  warnings              : "
                f"{' | '.join(env.warnings) or 'none'}",
            ]

        if scenario is not None:
            lines += ["", "SCENARIO", f"  {scenario.title}: {scenario.intent}"]
            if scenario.ambiguities:
                lines.append("  known ambiguities: "
                             + "; ".join(scenario.ambiguities))

        plan = state.get("plan")
        if plan is not None:
            lines += ["", f"PLAN (revision {plan.revision})",
                      f"  strategy: {plan.strategy}"]
            if plan.replan_reason:
                lines.append(f"  this was already a REPLAN because: "
                             f"{plan.replan_reason}")

        lines += ["", "STEP-BY-STEP RESULTS (in order)"]
        for result in results:
            marker = " <-- FIRST FAILURE" if result is failing else ""
            lines.append(
                f"  [{result.step.id}] {result.step.kind.value} "
                f"{result.step.target or ''}{marker}")
            lines.append(f"      expectation: "
                         f"{result.step.expectation or '(none declared)'}")
            lines.append(
                f"      hardware_ok={result.hardware_ok} "
                f"expectation_met={result.expectation_met} "
                f"silent_failure={result.silent_failure} "
                f"optional={result.step.optional}")
            if result.reasoning:
                lines.append(f"      judgement: {result.reasoning[:400]}")
            obs = result.observation
            if obs is not None:
                lines.append(
                    f"      screen_changed={obs.screen_changed} "
                    f"(ratio={obs.change_ratio}) "
                    f"focus={obs.focused_window} pad_state={obs.pad_state}")
                if obs.screen_description:
                    lines.append(f"      saw: {obs.screen_description[:400]}")
                if obs.screen_text:
                    lines.append(f"      OCR: {obs.screen_text[:400]}")
                if obs.log_excerpt:
                    lines.append("      logs: " + " / ".join(
                        obs.log_excerpt.splitlines()[:8]))
                if obs.notes:
                    lines.append("      notes: " + " | ".join(obs.notes))

        # The single most useful fact for ruling causes out: did ANYTHING work?
        worked = [r.step.id for r in results
                  if r.observation and r.observation.screen_changed]
        lines += ["", "KEY DISCRIMINATOR",
                  f"  steps that visibly changed the screen: "
                  f"{', '.join(worked) if worked else 'NONE'}"]
        if worked:
            lines.append("  -> the whole input chain (USB, UART, firmware, OTG "
                         "host mode, HID enumeration, Android routing) was "
                         "PROVEN WORKING during this session, so a later "
                         "failure is unlikely to be caused by those layers")
        else:
            lines.append("  -> NO step ever changed the screen, so the chain was "
                         "never proven end-to-end in this session; suspect the "
                         "upstream links before suspecting xCloud")
        return "\n".join(lines)

    # -- retry policy ------------------------------------------------------
    def _mechanical_retryable(self, analysis: RootCauseAnalysis,
                              state: GraphState) -> tuple[bool, str | None]:
        allowed = {str(c).lower() for c in
                   self.s.get_list("retry.retryable_causes",
                                   ["timing", "flaky_ui", "network",
                                    "stream_latency"])}
        cause = analysis.primary.cause_class.value.lower()
        replans = int(state.get("replans", 0))
        budget = int(self.s.get("retry.max_replans", 2))

        if replans >= budget:
            return False, (f"the replan budget ({budget}) is spent; "
                           f"further attempts would not add information")
        if cause not in allowed:
            return False, None
        return True, (f"cause '{cause}' is transient in nature, so a replan with "
                      f"longer waits and an extra checkpoint before the failing "
                      f"step is worth one attempt")

    # -- fallback ----------------------------------------------------------
    def _mechanical_analysis(self, state: GraphState,
                             results: list[StepResult],
                             failing: StepResult | None) -> RootCauseAnalysis:
        """No-LLM fallback: walk the causal chain in code.

        This encodes the parent project's hard-won troubleshooting table. It is
        coarse, but it is never wrong about the ORDER of the chain, which is the
        part people get wrong by jumping straight to "xCloud is broken".
        """
        env = state.get("environment")
        pad = env.pad if env else None
        anything_worked = any(r.observation and r.observation.screen_changed
                              for r in results)

        cause = CauseClass.UNKNOWN
        layer = "unknown"
        statement = "the failure could not be attributed automatically"
        test = ""
        recommendations: list[str] = []

        if pad is not None and not pad.link_open:
            cause, layer = CauseClass.WIRING, "wiring"
            statement = ("the PC never established the command link to the "
                         "board, so no input could be sent at all")
            test = ("run `python host\\pad_link.py --check`. A PONG proves this "
                    "hypothesis wrong and moves the fault downstream.")
            recommendations = [
                "confirm FT232RL TX -> Leonardo D0 and RX -> D1 (they must CROSS)",
                "confirm a common GND between the two boards",
                "confirm the FT232RL voltage jumper is on 5V, not 3.3V",
                "close the Arduino Serial Monitor - only one process may hold "
                "the port",
            ]
        elif pad is not None and pad.pad_connected_to_phone is False:
            cause, layer = CauseClass.HOST_MODE, "phone"
            statement = ("the board is alive but no USB host has enumerated the "
                         "pad, so the phone never received any HID report - "
                         "every command would still answer OK")
            test = ("look at the Leonardo's ON LED. Lit = the phone IS in host "
                    "mode and this hypothesis is wrong. Dark = confirmed, "
                    "because in host mode the phone powers the board.")
            recommendations = [
                "move the OTG adapter to the PHONE end of the cable, not the "
                "board end - with it at the board end both sides try to be host "
                "and nothing enumerates, silently",
                "use a USB DATA cable, not a charge-only one",
                "re-plug to force re-enumeration",
            ]
        elif not anything_worked:
            cause, layer = CauseClass.HID_ENUMERATION, "firmware"
            statement = ("commands were accepted throughout but the screen never "
                         "changed once, which is the classic signature of an HID "
                         "interface that never enumerated")
            test = ("plug the Leonardo into the PC instead of the phone and run "
                    "`python host\\verify_hid_raw.py`. 8/8 proves the pad is fine "
                    "and moves the fault to the phone/PWA side; fewer than 8/8 "
                    "confirms this hypothesis.")
            recommendations = [
                "run host\\verify_hid_raw.py - it reads REAL HID report bytes, "
                "so unlike a firmware OK it is capable of saying no",
                "confirm the phone shows a gamepad in a gamepad-tester app "
                "before blaming xCloud",
            ]
        elif failing is not None and failing.silent_failure:
            # Input demonstrably arrived earlier, so the upstream chain is proven
            # good and the fault is genuinely downstream.
            cause, layer = CauseClass.PWA_NOT_LOADED, "browser_pwa"
            statement = ("input reached the phone earlier in this run, so the "
                         "hardware chain works; this step's input was accepted "
                         "but the page did not react, so the PWA probably does "
                         "not have focus or is not on the expected screen")
            test = ("check the focused window and the screenshot for a dialog, "
                    "an app chooser or a keyboard. If the browser has focus and "
                    "shows xCloud, this hypothesis is wrong and an app defect "
                    "becomes plausible.")
            recommendations = [
                "confirm the xCloud page is loaded and signed in",
                "dismiss any permission dialog or app chooser stealing input",
                "raise execution.observe_delay_seconds - the cloud UI animates "
                "and adds 60-100 ms of network latency on top",
            ]
        elif failing is not None and not failing.hardware_ok:
            cause, layer = CauseClass.FIRMWARE, "firmware"
            statement = (f"the board refused the command for step "
                         f"{failing.step.id}")
            test = ("compare the `hid:` names in config/controls.yaml against "
                    "BTN_TABLE in the sketch; a mismatch confirms this.")
            recommendations = [
                "read the ERR reason in the step output - it names the fault",
                "re-flash with 1-FLASH.bat if the firmware string looks wrong",
            ]
        else:
            cause, layer = CauseClass.TIMING, "xcloud"
            statement = ("input reached the app but the expected change was not "
                         "observed in time, which most often means the wait was "
                         "shorter than the UI animation plus stream latency")
            test = ("re-run with a longer observe delay. If it then passes, this "
                    "was timing; if it fails identically, timing is ruled out "
                    "and an app defect becomes the leading hypothesis.")
            recommendations = [
                "raise execution.observe_delay_seconds",
                "raise the step interval - a cloud UI drops fast repeats",
            ]

        primary = Hypothesis(
            cause_class=cause, statement=statement, likelihood=0.6,
            supporting_evidence=[
                f"pad link open: {pad.link_open if pad else 'unknown'}",
                f"host enumerated the pad: "
                f"{pad.pad_connected_to_phone if pad else 'unknown'}",
                f"any step visibly changed the screen: {anything_worked}",
            ],
            discriminating_test=test)

        return RootCauseAnalysis(
            primary=primary, layer=layer,  # type: ignore[arg-type]
            failure_step_id=failing.step.id if failing else None,
            narrative=("Mechanical analysis: no LLM was available, so the causal "
                       "chain was walked in code from the PC outwards and the "
                       "fault attributed to the furthest upstream link the "
                       "evidence supports. Treat it as a starting point, and run "
                       "the discriminating test before acting on it."),
            recommendations=recommendations)
