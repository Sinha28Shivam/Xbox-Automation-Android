"""
launcher.py - open the xCloud page, and invalidate the handshake when it does.

WHY THIS IS ITS OWN NODE
========================
In closed-loop mode nothing else launches the PWA. The planner was demoted to
deriving the goal, so it no longer emits a `LAUNCH_PWA` step, and the loop would
otherwise begin by observing whatever happened to be on the phone already. That
worked by accident when a browser was left open by hand, and failed silently the
moment it was not.

THE FLAG RESET IS THE POINT
===========================
`handshake_done = False` on every successful launch. That single line is what
makes "hand shake after every page load" automatic rather than remembered.

It matters because the requirement is not "hand shake once at startup" - it is
"hand shake after every page load". A fresh page cannot see the gamepad until
the pad sends a button report (the W3C Gamepad API hides it deliberately), and
that applies just as much to a mid-run reload, a crashed tab, or a session that
bounced back to the library. Tying the reset to the launch rather than to the
start of the run means the loop cannot forget.

xCLOUD IS A URL, NOT AN APP
===========================
There is no package to start and no activity to assert on, so this sends a VIEW
intent and then waits for a network page load - which is why the wait here is
seconds rather than milliseconds, and why it goes through `ctx.timing` like every
other wait instead of being an invisible gap in the transcript.
"""

from __future__ import annotations

from ..logbook import log
from ..state import GraphState
from .base import Agent


class LauncherAgent(Agent):
    """Opens the xCloud PWA. Skippable for a browser already open by hand."""

    name = "launcher"

    def run(self, state: GraphState) -> GraphState:
        mode = str(self.s.get("android.pwa.launch_mode", "view_url")).lower()
        url = str(self.s.get("android.pwa.url", "https://www.xbox.com/play"))

        # -- already open by hand ---------------------------------------
        #
        # Still resets the handshake flag. The page may have been sitting there
        # for an hour, or been reloaded, and we cannot know whether it has ever
        # seen a button report. Assuming it has is precisely the assumption that
        # produces a run full of false silent failures.
        if mode in ("already_open", "manual"):
            log.hw(f"PWA launch skipped (android.pwa.launch_mode={mode!r}): the "
                   f"page is expected to be open already. The signal handshake "
                   f"still runs, because a page that has not received a button "
                   f"report cannot see the pad however long it has been open.",
                   indent=0)
            return {
                "handshake_done": False,
                "handshake_attempts": 0,
                "agent_trace": [self.trace(
                    "launch", f"skipped, launch_mode={mode}")],
            }

        android = self.ctx.android
        if android is None or not android.status.adb_available:
            # Not fatal. Without adb we cannot open the page, but a page opened
            # by hand is a perfectly valid rig - and the DeviceAgent has already
            # warned that verdicts are capped without screenshots.
            log.warn("cannot launch the PWA: adb is unavailable. Open "
                     f"{url} on the phone by hand. The handshake will still "
                     f"run.", indent=0)
            return {
                "handshake_done": False,
                "handshake_attempts": 0,
                "adaptations": [
                    "the PWA could not be launched (no adb), so the run assumes "
                    "the page is already open on the phone"],
                "agent_trace": [self.trace("launch", "no adb, not launched")],
            }

        log.hw(f"launching the xCloud PWA: {url}", indent=0)
        ok, detail = android.launch_pwa(url)

        if not ok:
            log.warn(f"the PWA launch intent failed: {detail}", indent=1)
            return {
                "handshake_done": False,
                "handshake_attempts": 0,
                "adaptations": [f"PWA launch failed: {detail}"],
                "agent_trace": [self.trace("launch", f"failed: {detail}")],
            }

        # A page load over the network, not an app start. Nothing can be judged
        # - or handed shaken - before it arrives.
        self.ctx.timing.sleep(
            float(self.s.get("android.pwa.settle_seconds", 6.0)),
            "android.pwa.settle_seconds - a PWA is a page load over the "
            "network, so neither the handshake nor any observation means "
            "anything before the page exists")

        log.ok(f"PWA launched. The signal handshake is now REQUIRED: this page "
               f"has never received a button report, so it cannot see the pad "
               f"yet.", indent=1)

        return {
            # THE reset. Every launch invalidates any previous handshake.
            "handshake_done": False,
            "handshake_attempts": 0,
            # `detail` is appended to the trace text, NOT passed as a keyword:
            # `Agent.trace(action, detail="", **extra)` already owns that name,
            # so `trace(a, b, detail=c)` is a TypeError.
            "agent_trace": [self.trace(
                "launch",
                f"opened {url}"
                + (f" - {detail[:200]}" if detail else ""))],
        }


