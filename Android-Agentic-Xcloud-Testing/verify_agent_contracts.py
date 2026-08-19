"""
verify_agent_contracts.py - every agent must be CALLABLE, not just importable.

THE BUG THIS GUARDS
===================
The handshake agent crashed on its first real run:

    TypeError: Agent.trace() got multiple values for argument 'detail'

`Agent.trace(action, detail="", **extra)` already owns the name `detail`, so
`self.trace("handshake", "some text", detail=other)` passes it twice. It is a
one-line mistake that no import check, no compile, and no unit test of the
agent's LOGIC would catch - it only fires when the line actually executes.

And when it fired, it cost a full minute: the pad opened, the scenario was
interpreted by an LLM, the PWA was launched, and only THEN did the run die -
producing a report whose summary confidently blamed rig misconfiguration for
what was actually a typo in our own trace call.

So this suite calls `trace()` the way each agent calls it, and executes each
agent's `run()` against a minimal state to prove the happy path and the early
returns are at least reachable.

Run:  python verify_agent_contracts.py
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentic import agents as agents_pkg                          # noqa: E402
from agentic.settings import Settings                             # noqa: E402
from agentic.state import trace as trace_fn                       # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"\n         {detail}" if detail else ""))


settings = Settings(use_dotenv=False)


# ==========================================================================
print("\n1. NO AGENT PASSES `detail=` TO trace() ALONGSIDE A POSITIONAL")
# ==========================================================================
# Read the source rather than running it: this catches the mistake on EVERY
# code path, including the ones a happy-path test never reaches.
sig = inspect.signature(trace_fn)
params = list(sig.parameters)
check(params[:3] == ["agent", "action", "detail"],
      "state.trace(agent, action, detail, **extra) - signature confirmed",
      f"parameters: {params}")

offenders: list[str] = []
for path in sorted((ROOT / "agentic").rglob("*.py")):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        offenders.append(f"{path.name}: unparseable ({exc})")
        continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", ""))
        if name != "trace":
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        # `self.trace(action, ...)` has 1 implicit-positional budget before
        # `detail`; `trace(agent, action, ...)` has 2.
        is_method = isinstance(func, ast.Attribute) and not (
            isinstance(func.value, ast.Name) and func.value.id == "trace")
        limit = 1 if is_method else 2
        if "detail" in kwargs and len(node.args) > limit:
            offenders.append(
                f"{path.relative_to(ROOT)}:{node.lineno} passes detail= with "
                f"{len(node.args)} positional args")

check(not offenders,
      "no trace() call passes `detail` both positionally and by keyword",
      "; ".join(offenders) if offenders else
      "this is the exact crash that killed run 20260819-155515")


# ==========================================================================
print("\n2. EVERY AGENT'S trace() HELPER ACTUALLY WORKS")
# ==========================================================================


class _Ctx:
    def __init__(self) -> None:
        self.settings = settings
        self.llm = type("L", (), {"calls": 0, "errors": [], "available": False,
                                  "supports_vision": lambda *a: False})()
        self.pad = self.android = self.vision = None
        self.run_id = "contract"
        self.last_frame_path = None
        self.artifacts: list[str] = []
        from agentic.timing import Timing
        self.timing = Timing(settings)
        self.state_builder = self.validator = None

    def elapsed(self) -> float:
        return 0.0

    def out_of_time(self) -> bool:
        return False


AGENT_CLASSES = [
    getattr(agents_pkg, name) for name in agents_pkg.__all__
    if name.endswith("Agent") and name != "Agent"
]
check(len(AGENT_CLASSES) >= 10,
      f"{len(AGENT_CLASSES)} agents discovered from agents.__all__",
      ", ".join(c.__name__ for c in AGENT_CLASSES))

for cls in AGENT_CLASSES:
    try:
        agent = cls(_Ctx())                                       # type: ignore[arg-type]
        # The three shapes used across the codebase.
        agent.trace("action")
        agent.trace("action", "a detail string")
        agent.trace("action", "a detail string", extra_key="extra value")
        check(True, f"{cls.__name__}.trace() accepts every shape used in-tree")
    except Exception as exc:                                      # noqa: BLE001
        check(False, f"{cls.__name__}.trace() raised",
              f"{type(exc).__name__}: {exc}")


# ==========================================================================
print("\n3. THE BOOTSTRAP AGENTS RUN WITHOUT A PHONE OR A BOARD")
# ==========================================================================
# Both must degrade rather than crash: no adb, no pad, no LLM. This is the path
# the failed run actually took, so it is the one most worth exercising.
from agentic.agents import HandshakeAgent, LauncherAgent           # noqa: E402

probe = Settings(use_dotenv=False)
probe.override("android.pwa.launch_mode", "already_open")
ctx = _Ctx()
ctx.settings = probe

try:
    out = LauncherAgent(ctx).run({})                              # type: ignore[arg-type]
    check(isinstance(out, dict) and "agent_trace" in out,
          "LauncherAgent.run() survives no-adb and returns a trace",
          f"handshake_done={out.get('handshake_done')}")
except Exception as exc:                                          # noqa: BLE001
    check(False, "LauncherAgent.run() crashed", f"{type(exc).__name__}: {exc}")

try:
    out = HandshakeAgent(_Ctx()).run({})                          # type: ignore[arg-type]
    check(isinstance(out, dict) and "agent_trace" in out,
          "HandshakeAgent.run() survives no-pad and returns a trace",
          "with no pad there is nothing to hand shake, and that is reported "
          "rather than raised")
except Exception as exc:                                          # noqa: BLE001
    check(False, "HandshakeAgent.run() crashed", f"{type(exc).__name__}: {exc}")

# And the disabled path.
off = Settings(use_dotenv=False)
off.override("execution.closed_loop.handshake.enabled", False)
ctx_off = _Ctx()
ctx_off.settings = off
try:
    out = HandshakeAgent(ctx_off).run({})                         # type: ignore[arg-type]
    check(out.get("handshake_done") is True,
          "a disabled handshake reports done and does not block the loop")
except Exception as exc:                                          # noqa: BLE001
    check(False, "HandshakeAgent disabled path crashed",
          f"{type(exc).__name__}: {exc}")


# ==========================================================================
print("\n4. THE NAVIGATION TARGET IS A GAME, NOT A URL")
# ==========================================================================
from agentic.graph import _target_from_scenario                   # noqa: E402
from agentic.schemas import ScenarioSpec                          # noqa: E402

# A scenario whose STEPS carry `target: <url>` for LAUNCH_PWA. The url appears
# first in the file, which is exactly why the naive scan picked it.
raw = """
id: minecraft_dungeons_launch
title: Launch Minecraft Dungeons and reach the main menu
steps:
  - action: LAUNCH_PWA
    target: "https://www.xbox.com/play"
  - action: NAVIGATE_TO_GAME
    target: "Minecraft Dungeons"
"""
spec = ScenarioSpec(title="Launch Minecraft Dungeons and reach the main menu",
                    intent="launch it with the gamepad")
target = _target_from_scenario({"scenario": spec, "raw_scenario": raw})

check(target is not None and not str(target).startswith("http"),
      "a LAUNCH_PWA url is NOT mistaken for the navigation target",
      f"target = {target!r}")
check(target == "Minecraft Dungeons",
      "the real game title is chosen instead",
      "a run hunting for 'https://www.xbox.com/play' on screen would never "
      "focus the tile, and every navigation decision after that is wrong")

# A goal TYPE is not a title either.
raw2 = """
title: Launch Forza Horizon and reach the main menu
goal:
  type: reach_main_menu
  game: Forza Horizon
"""
t2 = _target_from_scenario({"scenario": ScenarioSpec(
    title="Launch Forza Horizon and reach the main menu", intent="x"),
    "raw_scenario": raw2})
check(t2 == "Forza Horizon",
      "`game:` is read, and `type: reach_main_menu` is skipped",
      f"target = {t2!r}")


failed = [r for r in results if not r[0]]
print("\n" + "=" * 74)
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("\nFAILURES:")
    for _, name, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print("Agents are callable, not merely importable - the class of bug that only")
print("appears when a line finally executes is now caught offline.")
print("=" * 74)
