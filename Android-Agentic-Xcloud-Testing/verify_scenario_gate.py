"""
verify_scenario_gate.py - the scenario gate must refuse for the RIGHT reasons.

THE BUG THIS GUARDS
===================
Run 20260819-154400 halted at the SCENARIO node. The pad was opened, the phone
was there, and then nothing happened: no launch, no handshake, no screenshot, no
input. The report said "inconclusive - nothing was tested".

The stated reasons for refusing were, verbatim:

    "The exact visual appearance of Minecraft Dungeons tiles, focus indicators,
     and UI elements may vary based on xCloud PWA version served by the server"
    "The time required for game loading and boot may vary based on network
     conditions and server load"
    "The exact wording of menu items (Play vs Play now vs Resume) may vary"
    "Whether Minecraft Dungeons appears in 'Jump back in', 'Recently Played',
     'Featured', or 'Game Pass' rail is not specified"

Every one of those is a description of UI VARIABILITY - and resolving UI
variability by looking at the screen is precisely what the closed loop does. The
scenario agent was applying a standard that made sense when the runner followed a
pre-written keystroke route (where not knowing the layout in advance really was
fatal) and is simply wrong now.

The gate must still be able to say no. What it must not do is refuse because the
button might say "Play now".

Run:  python verify_scenario_gate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentic.agents import ScenarioAgent                          # noqa: E402
from agentic.schemas import (AcceptanceCriterion, Capabilities,    # noqa: E402
                             ScenarioSpec)
from agentic.settings import Settings                             # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"\n         {detail}" if detail else ""))


settings = Settings(use_dotenv=False)


class _Ctx:
    def __init__(self) -> None:
        self.settings = settings
        self.llm = type("L", (), {"calls": 0, "errors": [],
                                  "available": True})()
        self.pad = self.android = self.vision = None
        self.run_id = "gate"
        self.last_frame_path = None
        self.artifacts: list[str] = []
        from agentic.timing import Timing
        self.timing = Timing(settings)
        self.state_builder = self.validator = None

    def elapsed(self) -> float:
        return 0.0

    def out_of_time(self) -> bool:
        return False


agent = ScenarioAgent(_Ctx())                                     # type: ignore[arg-type]

# A rig that can see: screenshots + OCR + input.
caps = Capabilities(buttons=["a", "b", "up", "down", "left", "right", "guide"],
                    can_send_input=True, can_screenshot=True,
                    can_read_text=True, can_read_logs=True)


def spec_with(ambiguities: list[str], critical: bool = False) -> ScenarioSpec:
    return ScenarioSpec(
        title="Launch Minecraft Dungeons and reach the main menu",
        intent="launch the game with the gamepad and reach its own main menu",
        is_testable=False,
        ambiguities=list(ambiguities),
        acceptance_criteria=[
            AcceptanceCriterion(id="ac1",
                                statement="the game's main menu is visible",
                                observable_via=["screen_text",
                                                "screen_change"],
                                critical=critical)])


# ==========================================================================
print("\n1. THE EXACT REFUSAL FROM THE FAILED RUN IS OVERTURNED")
# ==========================================================================
real = [
    "The exact visual appearance of Minecraft Dungeons tiles, focus indicators, "
    "and UI elements may vary based on xCloud PWA version served by the server",
    "The time required for game loading and boot may vary based on network "
    "conditions and server load",
    "The exact wording of menu items (Play vs Play now vs Resume) may vary",
    "Whether Minecraft Dungeons appears in 'Jump back in', 'Recently Played', "
    "'Featured', or 'Game Pass' rail is not specified",
    "The exact appearance of the Minecraft Dungeons main menu may vary based on "
    "game version",
]
spec = spec_with(real)
agent._reconsider_refusal(spec, caps)

check(spec.is_testable,
      "the run is allowed to proceed",
      "all five reasons were UI variability, which observation resolves")
check(not spec.ambiguities,
      "the variability reasons no longer block the run")
check(len(spec.clarified_assumptions) == len(real),
      "they are preserved as ASSUMPTIONS, not discarded",
      f"{len(spec.clarified_assumptions)} recorded - the report still shows "
      f"what the run took for granted")
check(any("initially judged untestable" in n for n in spec.risk_notes),
      "the overturn itself is recorded in risk_notes",
      "a silently reversed judgement would be worse than the original bug")
check(any(c.critical for c in spec.acceptance_criteria),
      "a criterion the sensors CAN check is restored to critical",
      "otherwise the run proceeds but can never pass - a refusal by another name")


# ==========================================================================
print("\n2. IT CAN STILL SAY NO - AND DOES")
# ==========================================================================
for reasons, why in [
    (["the scenario requires checking the game's AUDIO output"],
     "audio cannot be seen"),
    (["verifying this needs frame-exact input timing"],
     "frame-exact timing is not achievable over a cloud stream"),
    (["it depends on a specific save file being present"],
     "a save file is not observable"),
    (["it asks for a subjective judgement of image quality"],
     "image quality is subjective"),
    (["measuring input lag requires latency measurement hardware"],
     "latency measurement is out of scope"),
    (["it needs controller rumble / haptic feedback to be felt"],
     "haptics cannot be observed by a camera"),
]:
    s = spec_with(reasons)
    agent._reconsider_refusal(s, caps)
    check(not s.is_testable, f"REFUSAL UPHELD: {why}",
          reasons[0][:80])

# The mixed case: variability AND a real blocker. One unobservable requirement
# is enough to refuse, so the refusal must stand.
mixed = spec_with([
    "the exact wording of the menu may vary",
    "and the test also requires checking the game's audio",
])
agent._reconsider_refusal(mixed, caps)
check(not mixed.is_testable,
      "variability + a REAL blocker = still refused",
      "one unobservable requirement outweighs any number of resolvable ones")
check(any("refusal upheld" in n for n in mixed.risk_notes),
      "and the upheld refusal says which blocker decided it")

# The unclassifiable case: we must NOT assume it is fine.
odd = spec_with(["the flux capacitor is misaligned"])
agent._reconsider_refusal(odd, caps)
check(not odd.is_testable,
      "an UNRECOGNISED reason leaves the refusal alone",
      "being unable to classify a reason is not a licence to ignore it")


# ==========================================================================
print("\n3. IT DOES NOT TOUCH A SCENARIO THAT WAS ALREADY FINE")
# ==========================================================================
fine = ScenarioSpec(title="ok", intent="ok", is_testable=True,
                    ambiguities=["the wording may vary"])
before = (fine.is_testable, list(fine.ambiguities),
          list(fine.clarified_assumptions), list(fine.risk_notes))
agent._reconsider_refusal(fine, caps)
check((fine.is_testable, fine.ambiguities, fine.clarified_assumptions,
       fine.risk_notes) == before,
      "an already-testable scenario is left completely untouched",
      "the guard only ever overturns a REFUSAL")

empty = ScenarioSpec(title="x", intent="x", is_testable=False, ambiguities=[])
agent._reconsider_refusal(empty, caps)
check(not empty.is_testable,
      "a refusal with NO stated reason is not overturned",
      "there is nothing to classify, so there is nothing to overrule")


# ==========================================================================
print("\n4. THE PROMPT AND THE CODE AGREE")
# ==========================================================================
src = (ROOT / "agentic/agents/scenario.py").read_text(encoding="utf-8")
check("UI VARIABILITY IS NOT A REASON TO REFUSE" in src,
      "the prompt states the rule explicitly")
check("_reconsider_refusal" in src and "_REAL_BLOCKERS" in src,
      "and the code enforces it, because a prompt is only a request")


failed = [r for r in results if not r[0]]
print("\n" + "=" * 74)
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("\nFAILURES:")
    for _, name, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print("The gate still refuses what cannot be seen, but no longer refuses a run")
print("because a button might say 'Play now' instead of 'Play'.")
print("=" * 74)
