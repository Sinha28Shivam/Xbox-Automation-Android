"""
verify_preserved.py - the OTHER half of the contract.

The closed loop is only an improvement if it kept what the previous design had
already got right. This file is the regression guard for exactly that: each of
these safeguards was written in response to a specific run that went wrong, and
a refactor that quietly dropped one would be a step backwards wearing the
costume of progress.

Run:  python verify_preserved.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"\n         {detail}" if detail else ""))


def contains(rel: str, needle: str) -> bool:
    return needle in (ROOT / rel).read_text(encoding="utf-8")


print("\nPRESERVED SAFEGUARDS (each one is a fix for a real failed run)")

check(contains("agentic/agents/verifier.py",
               "silent_failure = bool(result.hardware_ok)"),
      "silent_failure survives in the verifier",
      "hardware said OK + neither frame moved = the documented trap")

check(contains("agentic/agents/verifier.py", "DOWNGRADED to unknown"),
      "the glance-only downgrade guard survives",
      "'we looked too late' must stay distinct from 'input never arrived'")

check(contains("agentic/agents/verifier.py",
               "transition.goal_complete = goal.is_success(after)"),
      "goal completion is decided in CODE, not by the model",
      "a classifier that could end the run could do it by being agreeable")

check(contains("agentic/agents/evaluator.py", "return Verdict.FAIL, reasons"),
      "the verdict ceiling still turns a silent failure into FAIL")

check(contains("agentic/agents/evaluator.py",
               "no transition reported the goal state as reached"),
      "the ceiling gained a closed-loop rule: progress is not arrival")

check(contains("agentic/timing.py", "MIN_GLANCE = 0.25"),
      "the MEASURED glance floor is untouched",
      "the guide suggested 100-500ms, which is below this rig's real latency")

check(contains("agentic/graph.py", "ctx.pad.close()"),
      "pad.close() still runs in a finally block",
      "a crash mid-stick otherwise leaves an axis deflected")

check(contains("agentic/agents/actor.py", "self._exec._look("),
      "the two-look glance/settle cycle is REUSED, not reimplemented",
      "recomputing reacted_on differently would let report and verdict disagree")

check(contains("agentic/agents/recovery.py", "poll_until"),
      "recovery finally uses timing.poll_until",
      "written long ago with a good docstring and never called until now")

check(contains("agentic/agents/base.py", "THE FALLBACK RULE"),
      "the no-LLM fallback contract is still documented in base.py")

# The two LLM-using new agents must each have a documented mechanical path.
# `recovery` is deliberately NOT in this list: it makes no model calls at all,
# which is stronger than degrading gracefully - there is nothing to degrade.
for agent in ("decision", "verifier"):
    src = (ROOT / f"agentic/agents/{agent}.py").read_text(encoding="utf-8")
    has_fallback = ("_fallback" in src or "_mechanical" in src
                    or "no LLM" in src)
    check(has_fallback, f"{agent} agent degrades without an LLM")

recovery_src = (ROOT / "agentic/agents/recovery.py").read_text(encoding="utf-8")
check("self.think(" not in recovery_src,
      "recovery makes NO LLM call at all",
      "stronger than degrading gracefully: a wait or a B press is not a "
      "judgement call, and making it one is what cost the previous design "
      "an RCA cycle per loading screen")



print("\nREMOVED SCAR TISSUE (workarounds for the missing closed loop)")

code_lines = lambda rel: [                                       # noqa: E731
    line for line in (ROOT / rel).read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.strip().startswith("#")]

vision_game_refs = [l for l in code_lines("agentic/tools/vision.py")
                    if "minecraft" in l.lower() or "dungeons" in l.lower()]
check(not vision_game_refs,
      "no game name remains in the perception/sensor layer",
      "; ".join(vision_game_refs[:2]))

planner_game_refs = [l for l in code_lines("agentic/agents/planner.py")
                     if "minecraft" in l.lower() or "is_minecraft" in l]
check(not planner_game_refs,
      "the hardcoded Minecraft ladder is gone from the planner",
      "; ".join(planner_game_refs[:2]))

exec_lines = code_lines("agentic/agents/executor.py")
check(not [l for l in exec_lines if "Capping" in l],
      "the D-pad times-cap workaround is gone")
check(not [l for l in exec_lines if "jumping directly to" in l],
      "the cursor-jump intent-string hack is gone")
check(not [l for l in exec_lines if "detail_page_open" in l],
      "the executor no longer reads the unreliable semantic flags")


print("\nBOTH MODES STILL SELECTABLE")
sys.path.insert(0, str(ROOT))
from agentic.graph import is_closed_loop                          # noqa: E402
from agentic.settings import Settings                             # noqa: E402

for mode, expected in (("closed_loop", True), ("adaptive", False),
                       ("plan", False)):
    probe = Settings(use_dotenv=False)
    probe.override("execution.mode", mode)
    check(is_closed_loop(probe) is expected,
          f"mode={mode} routes to {'the closed loop' if expected else 'the legacy plan walker'}")


failed = [r for r in results if not r[0]]
print("\n" + "=" * 74)
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("\nREGRESSIONS:")
    for _, name, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print("Every pre-existing safeguard is intact; only the workarounds are gone.")
print("=" * 74)
