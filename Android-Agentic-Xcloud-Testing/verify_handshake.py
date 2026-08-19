"""
verify_handshake.py - prove the signal handshake is automatic and verified.

THE PROBLEM BEING TESTED
========================
Every fresh browser page starts deaf. The W3C Gamepad API hides a gamepad from a
page until the pad sends a button event, so xCloud cannot see the Leonardo until
guide x2 + B has run - and it must run again after EVERY page load, not once at
startup.

Before this change the handshake was a method in `device.py` that:
  * nothing ever called,
  * ran before the PWA launched (so the page load discarded it), and
  * set `guide_signal_verified = True` even when it detected nothing.

Run:  python verify_handshake.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentic.graph import (route_after_handshake,                # noqa: E402
                           route_after_launch, route_after_strategy)
from agentic.schemas import (Action, ActionType, Capabilities,     # noqa: E402
                             FailureClass, GameState,
                             RECOVERABLE_FAILURES, ScreenType)
from agentic.settings import Settings                             # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"\n         {detail}" if detail else ""))


settings = Settings(use_dotenv=False)


# ==========================================================================
print("\n1. THE SEQUENCE IS DECLARED IN YAML, NOT IN PYTHON")
# ==========================================================================
import yaml                                                       # noqa: E402

controls = yaml.safe_load(
    (ROOT.parent / "config" / "controls.yaml").read_text(encoding="utf-8"))
special = controls.get("special_actions", {})

check("signal_handshake" in special,
      "controls.yaml defines `signal_handshake`",
      "a phone needing three presses is a YAML edit, not a code change")

seq = special.get("signal_handshake", {}).get("sequence", [])
buttons = [s.get("button") for s in seq if "button" in s]
check(buttons.count("guide") == 2,
      "it presses Guide exactly twice",
      f"sequence buttons = {buttons}")
check(buttons and buttons[-1] == "b",
      "it ends with B",
      "the overlay must be dismissed so the first real observation is not "
      "measuring our own side effect")
check(special["signal_handshake"].get("verified") is True,
      "it is marked verified:true (measured on this rig)")

timing = controls.get("timing", {})
check("handshake_settle" in timing and "handshake_dismiss" in timing,
      "the handshake waits are named in controls.yaml timing",
      f"settle={timing.get('handshake_settle')}s "
      f"dismiss={timing.get('handshake_dismiss')}s")


# ==========================================================================
print("\n2. THE GRAPH RUNS IT AFTER EVERY LAUNCH, BEFORE ANY OBSERVATION")
# ==========================================================================
check(route_after_strategy({}) == "observe",
      "strategy routes onward (mapped to `launch` in the closed-loop graph)")

check(route_after_launch({}) == "handshake",
      "launch -> handshake, UNCONDITIONALLY",
      "observing first would photograph a page that cannot see the pad")

check(route_after_handshake({"handshake_done": True}) == "observe",
      "handshake -> observe once confirmed")
check(route_after_handshake({"handshake_done": False}) == "handshake",
      "handshake retries ITSELF while unconfirmed",
      "the page is fine; reloading would throw away a good page load")
check(route_after_handshake({"handshake_done": False,
                            "halt_reason": "budget spent"}) == "report",
      "an exhausted handshake budget reaches the report, never spins")


# ==========================================================================
print("\n3. A LAUNCH INVALIDATES ANY PREVIOUS HANDSHAKE")
# ==========================================================================
from agentic.agents import LauncherAgent                          # noqa: E402


class _Ctx:
    """No hardware, no adb, no LLM."""
    def __init__(self, adb: bool = False) -> None:
        self.settings = settings
        self.llm = type("L", (), {"calls": 0, "errors": []})()
        self.pad = None
        self.vision = None
        self.run_id = "verify"
        self.last_frame_path = None
        self.artifacts: list[str] = []
        from agentic.timing import Timing
        self.timing = Timing(settings)
        self.state_builder = None
        self.validator = None
        status = type("S", (), {"adb_available": adb})()
        self.android = type("A", (), {"status": status})() if adb is not None else None

    def elapsed(self) -> float:
        return 0.0

    def out_of_time(self) -> bool:
        return False


probe = Settings(use_dotenv=False)
probe.override("android.pwa.launch_mode", "already_open")
ctx = _Ctx()
ctx.settings = probe
launcher = LauncherAgent(ctx)                                     # type: ignore[arg-type]
out = launcher.run({"handshake_done": True, "handshake_attempts": 3})

check(out.get("handshake_done") is False,
      "an ALREADY-OPEN page still resets handshake_done",
      "a page open for an hour may never have received a button report")
check(out.get("handshake_attempts") == 0,
      "the attempt counter resets too, so the retry budget is per page load")

ctx_noadb = _Ctx(adb=False)
out2 = LauncherAgent(ctx_noadb).run({})                           # type: ignore[arg-type]
check(out2.get("handshake_done") is False,
      "a launch that could not run (no adb) still demands a handshake")
check("halt_reason" not in out2,
      "and it does NOT halt the run - a hand-opened page is a valid rig")


# ==========================================================================
print("\n4. A LOST PAD MID-RUN IS RECOGNISED AND RE-HANDSHAKEN")
# ==========================================================================
from agentic.agents import DecisionAgent, RecoveryAgent           # noqa: E402
from agentic.agents.observer import derive_goal                   # noqa: E402
from agentic.schemas import ScenarioSpec                          # noqa: E402

caps = Capabilities(
    buttons=["a", "b", "x", "y", "up", "down", "left", "right", "guide"],
    special_actions={"signal_handshake": "make the pad visible to the browser"},
    can_send_input=True, can_screenshot=True)

goal = derive_goal(ScenarioSpec(title="Launch a game and reach the main menu",
                                intent="launch and reach the main menu"),
                   settings)

decider = DecisionAgent(_Ctx())                                   # type: ignore[arg-type]

prompt_state = GameState(screen_type=ScreenType.CONTROLLER_PROMPT,
                         controller_prompt=True, confidence=0.9)
action, why = decider._deterministic(prompt_state, goal, caps)
check(action is not None and action.type is ActionType.MACRO
      and action.control == "signal_handshake",
      "a CONTROLLER_PROMPT triggers the handshake macro, with no LLM call",
      why)

# The deadlock case: a controller prompt drawn over a loading screen.
both = GameState(screen_type=ScreenType.CONTROLLER_PROMPT,
                 controller_prompt=True, loading=True, confidence=0.9)
action2, why2 = decider._deterministic(both, goal, caps)
check(action2 is not None and action2.type is ActionType.MACRO,
      "a controller prompt OVER a loading screen still hand shakes",
      "waiting for a screen that is waiting for US is a deadlock")

check(FailureClass.INPUT_IGNORED in RECOVERABLE_FAILURES,
      "INPUT_IGNORED is recoverable",
      "it is the exact signature of a page that has lost the pad: firmware OK, "
      "nothing moved")
check(FailureClass.CONTROLLER_NOT_DETECTED in RECOVERABLE_FAILURES,
      "CONTROLLER_NOT_DETECTED is recoverable")

recovery = RecoveryAgent(_Ctx())                                  # type: ignore[arg-type]
strategy, r_action, note = recovery._strategy(
    FailureClass.INPUT_IGNORED, None, caps)
check(r_action is not None and r_action.type is ActionType.MACRO
      and r_action.control == "signal_handshake",
      "recovery answers INPUT_IGNORED by re-announcing the pad",
      f"{strategy}: {note}")


# ==========================================================================
print("\n5. IT IS VERIFIED, AND CAN SAY NO")
# ==========================================================================
from agentic.agents.handshake import (HandshakeAgent, OVERLAY_CUES,  # noqa: E402
                                      PROMPT_CUES)

check(settings.get("execution.closed_loop.handshake.verify") is True,
      "verification is on by default")
check(settings.get("execution.closed_loop.handshake.enabled") is True,
      "the handshake itself is on by default")
check(int(settings.get("execution.closed_loop.handshake.max_attempts", 0)) >= 1,
      "there is a finite attempt budget",
      f"max_attempts={settings.get('execution.closed_loop.handshake.max_attempts')}")


class _Vision:
    """Fake sensors, so the inspection logic can be tested without a phone."""
    def __init__(self, text: str = "", ratio: float | None = 0.0) -> None:
        self._text, self._ratio = text, ratio
        self.can_screenshot = True

    def ocr(self, _path: object) -> str:
        return self._text

    def diff(self, _a: object, _b: object) -> float | None:
        return self._ratio


def inspect(text: str, ratio: float | None) -> tuple[bool, str]:
    ctx = _Ctx()
    ctx.vision = _Vision(text, ratio)                             # type: ignore[assignment]
    return HandshakeAgent(ctx)._inspect("before.png", "after.png")  # type: ignore[arg-type]


ok, why = inspect("Settings   Friends   Quit game", 0.0)
check(ok, "the Guide overlay's own words prove the pad was seen", why[:90])

ok, why = inspect("", 0.42)
check(ok, "a large frame change also proves it", why[:90])

ok, why = inspect("Connect a controller to play", 0.55)
check(not ok,
      "a 'connect a controller' banner is a FAILURE even when pixels moved",
      "this is the case a frame-diff-only check gets exactly backwards")

ok, why = inspect("", 0.0)
check(not ok, "a still screen with no overlay text is honestly reported as "
              "unverified", why[:90])

ok, why = inspect("", None)
check(not ok, "no frame comparison available -> unverified, not assumed",
      why[:90])

check(any("connect a controller" in c for c in PROMPT_CUES),
      "the prompt cue list covers the common wording")
check("settings" in OVERLAY_CUES and "quit" in OVERLAY_CUES,
      "the overlay cue list is generic console vocabulary, not game-specific")


# ==========================================================================
print("\n6. THE DEAD CODE IS GONE")
# ==========================================================================
device_src = (ROOT / "agentic/agents/device.py").read_text(encoding="utf-8")
code = [l for l in device_src.splitlines()
        if l.strip() and not l.strip().startswith("#")]
check(not [l for l in code if "_verify_guide_handshake" in l],
      "device.py no longer carries the never-called handshake method")
check("_verify_guide_handshake" in device_src,
      "but a comment records WHY it was removed",
      "wrong place (pre-launch) and it could not say no")


failed = [r for r in results if not r[0]]
print("\n" + "=" * 74)
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("\nFAILURES:")
    for _, name, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print("The handshake now runs after EVERY page load, is verified against the")
print("screen, and re-runs itself if the page ever loses the pad mid-run.")
print("=" * 74)
