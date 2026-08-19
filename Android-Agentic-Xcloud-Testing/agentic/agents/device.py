"""
device.py - AGENT 1: "is the phone connected and can we send it signals?"

The gatekeeper. It runs first and can stop the whole graph, because a test that
runs against a dead link does not produce a failure - it produces a lie.

It answers three separate questions that are easy to conflate:

  1. Does the BOARD answer?          (PING -> PONG, over the FT232RL UART)
  2. Has a HOST enumerated the pad?  (the phone in OTG host mode)
  3. Can we SEE the phone?           (adb, optional)

Conflating 1 and 2 is the specific bug the parent project spent real time on: the
firmware answered `OK` to everything while its HID interface had never
enumerated. So each is reported on its own, and the pad's own `pad_connected`
bit - a check capable of saying no - is treated as the authority on question 2.

Note the asymmetry deliberately encoded in `_mechanical_readiness`: input is
REQUIRED, observation is not. Without input there is nothing to test. Without
observation we can still test, we just cannot see - which downgrades verdicts
rather than blocking the run.
"""

from __future__ import annotations

from ..schemas import Capabilities, EnvironmentReport
from ..state import GraphState
from .base import Agent


class DeviceAgent(Agent):
    name = "device"

    def run(self, state: GraphState) -> GraphState:
        report = EnvironmentReport()

        # -- 1 + 2: the pad ------------------------------------------------
        report.pad = self.ctx.pad.connect()
        caps = self.ctx.pad.capabilities()

        # -- 3: the phone --------------------------------------------------
        report.android = self.ctx.android.connect()

        # -- what can we actually do? -------------------------------------
        vision = self.ctx.vision
        caps.can_screenshot = vision.can_screenshot
        caps.can_read_text = vision.can_ocr and vision.can_screenshot
        caps.can_read_logs = bool(report.android.adb_available
                                  and report.android.device_state == "device")
        caps.can_launch_pwa = caps.can_read_logs

        # can_adb_text is NOT simply "adb is available". Being able to talk to
        # the device says nothing about being allowed to INJECT INPUT into it:
        # on this Xiaomi/MIUI phone `adb shell input text` is refused with a
        # SecurityException (INJECT_EVENTS) while every other adb command works
        # perfectly. Worse, `input` exits ZERO when refused, so the only way to
        # know is to try it and read the output.
        #
        # It is probed here, once, for a reason: if the planner is told typing is
        # available when it is not, it will build a plan around a search step
        # that cannot work, and the run fails several minutes later pointing at
        # the wrong layer. Discovering it now turns that into an honest,
        # up-front capability gap.
        caps.can_adb_text = False
        if caps.can_read_logs and self.ctx.android is not None:
            can_inject, inject_detail = self.ctx.android.can_inject_events()
            caps.can_adb_text = can_inject
            if not can_inject:
                caps.unavailable.append(
                    "adb text/keyevent injection: the device refuses it "
                    "(INJECT_EVENTS). Everything else over adb works - "
                    "screenshots, logcat, launching the PWA - so this is a "
                    "PERMISSION gap, not a broken connection. Any scenario that "
                    "needs typing cannot run; one that navigates with the D-pad "
                    "can. " + inject_detail.splitlines()[0])

        caps.vision_model = self.llm.supports_vision("observer")

        for reason in vision.degraded_reasons:
            caps.unavailable.append(reason)
        if not caps.can_screenshot:
            caps.unavailable.append(
                f"screen observation: {report.android.error or 'adb unavailable'}")
        if not self.llm.available:
            caps.unavailable.append(
                "LLM reasoning: " + "; ".join(self.llm.errors[:2]))

        report.capabilities = caps

        # -- gate ----------------------------------------------------------
        self._mechanical_readiness(report)
        report.assessment = self._assess(report)

        return {
            "environment": report,
            "capabilities": caps,
            "halt_reason": None if report.ready else "; ".join(
                report.blocking_reasons),
            "agent_trace": [self.trace(
                "environment_check",
                f"ready={report.ready} pad={report.pad.link_open} "
                f"host_enumerated={report.pad.pad_connected_to_phone} "
                f"adb={report.android.adb_available}")],
            "errors": [] if report.ready else list(report.blocking_reasons),
        }

    # ----------------------------------------------------------------------
    def _mechanical_readiness(self, report: EnvironmentReport) -> None:
        """Decide readiness in code, not in a prompt.

        The gate must be deterministic: whether we touch hardware cannot depend
        on a model's mood, and it must behave identically with no API key.
        """
        pad, android = report.pad, report.android

        if not pad.link_open:
            report.blocking_reasons.append(
                f"no gamepad link: {pad.error or 'the board did not answer'}. "
                f"Without it no input can be sent, so nothing can be tested. "
                f"Check: FT232RL TX->D0 and RX->D1 must CROSS, GND is connected, "
                f"the FTDI jumper is on 5V, and no Serial Monitor holds the port.")

        if pad.pad_connected_to_phone is False:
            # Explicitly not a blocker when dry_run: the point of dry_run is to
            # exercise planning with no hardware attached at all.
            if not pad.dry_run:
                report.blocking_reasons.append(
                    "the board is alive but NO USB host has enumerated the pad, "
                    "so the phone is not receiving HID reports. Every command "
                    "would return OK and change nothing. Usual cause: the phone "
                    "is not in OTG host mode - the OTG adapter must be at the "
                    "PHONE end, and the Leonardo's ON LED must be lit.")

        if pad.dry_run:
            report.warnings.append(
                "DRY RUN: commands are printed, never sent. Any 'pass' verdict "
                "describes the plan only and says nothing about the device.")

        if not android.adb_available or android.device_state != "device":
            detail = android.error or "no adb device"
            if self.s.get("android.required", False):
                report.blocking_reasons.append(
                    f"android.required is true but adb is unusable: {detail}")
            else:
                # The important asymmetry: blind, not blocked.
                report.warnings.append(
                    f"NO SCREEN OBSERVATION: {detail} The run continues, but a "
                    f"firmware OK cannot prove the app reacted, so verdicts will "
                    f"be inconclusive rather than pass.")

        if android.screen_on is False:
            report.warnings.append(
                "the phone's screen appears to be OFF, which would explain any "
                "input having no visible effect")

        if android.adb_available and android.device_state == "device" \
                and not android.browsers_found:
            report.warnings.append(
                "no browser package matched android.pwa.browser_hints. xCloud is "
                "a PWA and needs one, so either the hints need widening or the "
                "page must already be open on the phone.")

        report.ready = not report.blocking_reasons

    # ----------------------------------------------------------------------
    def _assess(self, report: EnvironmentReport) -> str:
        """Plain-English read of the environment, with the fix to try."""
        facts = self._facts(report)
        role = ("You are a hardware bring-up specialist. Given the probe "
                "results, explain in at most 120 words what state the rig is in "
                "and the single most likely fix for anything wrong. Distinguish "
                "clearly between 'the board answers' and 'the phone receives HID "
                "reports' - they fail independently and have different fixes. "
                "Do not invent facts that are not in the data.")
        answer = self.llm_text(role, facts)
        return answer or facts

    def llm_text(self, role: str, user: str) -> str:
        try:
            text = self.llm.text(self.name, self.system_prompt(role), user)
            self.llm_used = True
            return text
        except Exception as exc:                     # noqa: BLE001
            self.notes.append(f"device: assessment prose unavailable ({exc})")
            return ""

    @staticmethod
    def _facts(report: EnvironmentReport) -> str:
        pad, android, caps = report.pad, report.android, report.capabilities
        lines = [
            "PAD LINK",
            f"  serial link open : {pad.link_open}",
            f"  port             : {pad.port}",
            f"  firmware         : {pad.firmware}",
            f"  transport        : {pad.transport}",
            f"  host enumerated the pad (phone in OTG host mode): "
            f"{pad.pad_connected_to_phone}",
            f"  dry run          : {pad.dry_run}",
            f"  error            : {pad.error}",
        ]
        if pad.diagnostics:
            lines.append("  diagnostics      : " + " | ".join(pad.diagnostics))
        lines += [
            "PHONE (adb, optional)",
            f"  adb available    : {android.adb_available} ({android.adb_version})",
            f"  device / state   : {android.device_serial} / {android.device_state}",
            f"  model / android  : {android.model} / {android.android_version}",
            f"  screen           : {android.screen_size}, on={android.screen_on}",
            f"  focused window   : {android.focused_window}",
            f"  browsers found   : {', '.join(android.browsers_found) or 'none'}",
            f"  webapks found    : {', '.join(android.webapks_found) or 'none'}",
            f"  error            : {android.error}",
            "CAPABILITIES",
            f"  {caps.summary_for_prompt()}",
        ]
        if report.blocking_reasons:
            lines.append("BLOCKING: " + " | ".join(report.blocking_reasons))
        if report.warnings:
            lines.append("WARNINGS: " + " | ".join(report.warnings))
        return "\n".join(lines)

    # NOTE: `_verify_guide_handshake` used to live here and has been REMOVED.
    #
    # It was ~45 lines that pressed Guide twice, looked for the overlay, and
    # pressed B - and it was never called by anything, so it had never once run.
    # Two things were wrong with it beyond that:
    #
    # 1. WRONG PLACE. DeviceAgent runs FIRST, before the PWA is launched. A
    #    handshake performed here is thrown away by the page load that follows,
    #    because the W3C Gamepad API hides the pad from each new page until the
    #    pad sends a button event. It has to happen AFTER the browser is up.
    #
    # 2. WRONG VERDICT. It set `guide_signal_verified = True` on BOTH branches,
    #    including the one where nothing was detected. A check that cannot say
    #    no is not a check, and this project's whole discipline is built on
    #    checks capable of saying no.
    #
    # It now lives in `agents/handshake.py` as a graph node that runs after
    # `launch`, repeats after every page load, and reports honestly when the
    # browser never acknowledged the pad. This agent still OWNS the
    # `guide_signal_verified` / `guide_verification_notes` fields on
    # EnvironmentReport - the handshake agent fills them in.


