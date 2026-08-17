"""
verify_timing.py - prove the timing/logging change does what it claims.

Run it with no hardware, no phone and no API key:

    python verify_timing.py

WHY THIS FILE EXISTS
--------------------
The parent project's lesson, applied to this change: a check that cannot say NO
is worth nothing. The fix here is a claim about WHEN we look at the screen, and
the only way to know it holds is to feed it the exact frame pattern that broke
run 20260817-105323 and confirm the verdict comes out different.

Case 3 below IS that run: the glance moved 3.257%, the settled frame moved
0.074%. The old harness saw only the second number and reported FAIL with a
silent-failure flag. If this script ever prints FAIL for case 3, the regression
is back.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentic.agents.executor import ExecutorAgent            # noqa: E402
from agentic.logbook import log                              # noqa: E402
from agentic.schemas import ActionKind, Observation          # noqa: E402
from agentic.settings import Settings                        # noqa: E402
from agentic.timing import MIN_GLANCE, Timing                # noqa: E402

failures: list[str] = []


def check(label: str, got: object, expected: object) -> None:
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}"
          + ("" if ok else f", expected {expected!r}"))
    if not ok:
        failures.append(label)


# ==========================================================================
print("=" * 72)
print("1. SETTLE PROFILES - every action kind resolves to a glance + a settle")
print("=" * 72)

settings = Settings()
timing = Timing(settings, rig_timing={"menu_transition_wait": 1.2,
                                      "screen_load_wait": 3.0})

for kind in ActionKind:
    profile = timing.profile_for(kind)
    print(f"  {kind.value:<12} {profile.describe():<52} [{profile.reason}]")
    if profile.glance < 0 or profile.settle < 0:
        failures.append(f"{kind.value} produced a negative delay")

print()
press = timing.profile_for(ActionKind.PRESS)
check("a press gets a non-zero glance (we look BEFORE the UI settles)",
      press.glance > 0, True)
check("the glance is shorter than the settle", press.glance < press.settle, True)
check("an OBSERVE step needs no glance (it IS the observation)",
      timing.profile_for(ActionKind.OBSERVE).glance, 0.0)
check("a WAIT step adds nothing (its duration is already the wait)",
      timing.profile_for(ActionKind.WAIT).total, 0.0)
check("launching the PWA waits longest - it is a network page load",
      timing.profile_for(ActionKind.LAUNCH_PWA).settle
      > timing.profile_for(ActionKind.PRESS).settle, True)

print()
print("  the rig's own controls.yaml timing is honoured over our defaults:")
print(f"    macro settle = {timing.profile_for(ActionKind.MACRO).settle}s "
      f"(controls.yaml menu_transition_wait=1.2, built-in default 2.0, "
      f"the LARGER wins)")

# ==========================================================================
print()
print("=" * 72)
print("2. THE LATENCY FLOOR - a glance below the network latency is not a glance")
print("=" * 72)

settings.override("execution.settle.press.glance", 0.01)
floored = Timing(settings).profile_for(ActionKind.PRESS)
check(f"a 0.01s glance is raised to the {MIN_GLANCE}s floor",
      floored.glance, MIN_GLANCE)
print(f"    reason carried into the report: {floored.reason}")
settings.override("execution.settle.press.glance", None)

print()
settings.override("execution.settle.scale", 2.0)
scaled = Timing(settings).profile_for(ActionKind.PRESS)
check("execution.settle.scale doubles every wait at once",
      round(scaled.settle, 2), round(press.settle * 2, 2))
settings.override("execution.settle.scale", 1.0)

# ==========================================================================
print()
print("=" * 72)
print("3. THE REGRESSION ITSELF - run 20260817-105323's frame pattern")
print("=" * 72)
print("""
  That run pressed the D-pad and xCloud responded, then the highlight settled
  back before the single screenshot was taken:

      glance  (t+0.45s)   3.257% changed   <- the proof the input arrived
      settled (t+2.00s)   0.074% changed   <- what the old harness measured

  The old code read the settled frame ALONE, called it screen_changed=False,
  set silent_failure=True and reported FAIL. It then sent its reader to
  investigate USB-OTG permissions for a fault that did not exist.
""")


def obs(ratio: float | None, changed: bool | None) -> Observation:
    o = Observation(change_ratio=ratio, screen_changed=changed)
    o.sensors_used = ["screenshot", "frame_diff"]
    return o


phase = ExecutorAgent._reaction_phase

cases = [
    ("the regression: transient reaction, settled frame still",
     obs(0.03257, True), obs(0.00074, False), "glance"),
    ("a persistent reaction - a screen that navigated and stayed",
     obs(0.31, True), obs(0.28, True), "both"),
    ("a genuinely dead screen: NOTHING moved at either moment",
     obs(0.0004, False), obs(0.0002, False), "neither"),
    ("a slow reaction that only arrived by the settled frame",
     obs(0.0003, False), obs(0.12, True), "settle"),
    ("no frame diff possible (no adb, no numpy) - must NOT claim a failure",
     obs(None, None), obs(None, None), "unknown"),
]

for label, glance, settled, expected in cases:
    check(label, phase(glance, settled), expected)

print()
print("  What each outcome now means for the verdict:")
print("    glance / settle / both -> the input REACHED xCloud. Not a silent")
print("                              failure, whichever frame saw it.")
print("    neither                -> the firmware said OK and nothing moved at")
print("                              EITHER moment. This is the real finding.")
print("    unknown                -> no sensor could answer. Capped at")
print("                              inconclusive, never reported as a pass.")

# ==========================================================================
print()
print("=" * 72)
print("4. WAITS ARE LOGGED AND ACCOUNTED FOR")
print("=" * 72)

log.configure(run_id="verify", level="info", file_path=None, colour=False)
t = Timing(Settings())
p = t.profile_for(ActionKind.PRESS)
t.glance(p)
t.settle(p)
t.sleep(0.05, "a named wait, so the transcript can explain the delay")

print()
check("every wait was recorded for the report", len(t.waits), 3)
check("the total is tracked, so 'where did the time go' is answerable",
      t.total_waited > 0, True)
print(f"  summary line the report will carry: {t.summary()}")

print()
print("  poll_until returns as soon as a condition holds, instead of sleeping")
print("  blindly - and reports a TIMEOUT rather than continuing as if it passed:")
calls = {"n": 0}


def ready() -> bool:
    calls["n"] += 1
    return calls["n"] >= 2


check("poll_until succeeds on a condition that becomes true",
      t.poll_until(ready, timeout=2.0, interval=0.1, reason="a test condition"),
      True)
check("poll_until reports failure honestly on timeout",
      t.poll_until(lambda: False, timeout=0.3, interval=0.1,
                   reason="a condition that never holds"),
      False)

# ==========================================================================
print()
print("=" * 72)
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    print("=" * 72)
    sys.exit(1)

print("ALL CHECKS PASSED")
print("=" * 72)
print("""
The claim this verifies: a step is now judged on TWO frames, and the only
outcome that counts as "nothing happened" is 'neither'. Run 20260817-105323's
nav_test yields 'glance', so its evidence is kept instead of discarded - and
its FAIL becomes a recorded reaction.
""")
sys.exit(0)
