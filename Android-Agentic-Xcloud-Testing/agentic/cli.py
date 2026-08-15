"""
cli.py - the command line.

    python -m agentic "press A and check the screen reacts"
    python -m agentic --scenario scenarios/controller_detected.yaml
    python -m agentic --scenario scenarios/ --all        # a whole suite
    python -m agentic --check                            # rig only, no test
    python -m agentic --capabilities                     # what can this run do?
    python -m agentic --dry-run "..."                    # plan, never touch HW

Every flag maps to a dotted config key via `Settings.override`, so there is
exactly ONE resolution path (flag > env > yaml > default) and no flag can mean
something different from its config key.

The exit code is the machine-readable verdict, for CI:
    0 pass   1 fail   2 blocked   3 inconclusive   4 error
`inconclusive` is deliberately NOT 0. A run that proved nothing must not be able
to turn a pipeline green.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agents.scenario import ScenarioAgent
from .graph import build_context, run_test
from .schemas import Verdict
from .settings import PACKAGE_ROOT, Settings

EXIT_CODES = {
    Verdict.PASS: 0,
    Verdict.FAIL: 1,
    Verdict.BLOCKED: 2,
    Verdict.INCONCLUSIVE: 3,
    Verdict.ERROR: 4,
}

SCENARIO_SUFFIXES = (".yaml", ".yml", ".md", ".txt")


def _say(message: str) -> None:
    """Print with an ASCII-safe fallback.

    The Windows console is cp1252 by default, and a stray non-encodable
    character in a model's prose would otherwise raise UnicodeEncodeError and
    crash the tool over a display concern - the same trap pad_link.py documents
    for corrupted serial bytes.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"))


# ==========================================================================
# Sub-commands that do not run a test
# ==========================================================================
def cmd_check(settings: Settings) -> int:
    """Probe the rig and print what the DeviceAgent found. No test, no input."""
    from .agents import DeviceAgent
    from .state import new_state

    ctx = build_context(settings, "check")
    state = new_state("", "<check>")
    try:
        result = DeviceAgent(ctx).run(state)
    finally:
        ctx.pad.close()

    env = result["environment"]
    _say("=" * 68)
    _say("RIG CHECK")
    _say("=" * 68)
    _say(DeviceAgent(ctx)._facts(env))               # noqa: SLF001
    _say("")
    _say("ASSESSMENT")
    _say(env.assessment or "(no assessment available)")
    _say("")
    if env.ready:
        _say("READY. Reminder: this proves the BOARD answers and whether a HOST")
        _say("has enumerated the pad. It does NOT prove xCloud reacts - only a")
        _say("screen observation can do that.")
        return 0
    _say("NOT READY:")
    for reason in env.blocking_reasons:
        _say(f"  - {reason}")
    return 2


def cmd_capabilities(settings: Settings) -> int:
    """Print the capability list the agents will be given. Nothing is hardcoded:
    this is read live from ../config/controls.yaml plus sensor probing."""
    from .agents import DeviceAgent
    from .state import new_state

    ctx = build_context(settings, "capabilities")
    try:
        result = DeviceAgent(ctx).run(new_state("", "<capabilities>"))
    finally:
        ctx.pad.close()

    caps = result["capabilities"]
    _say("=" * 68)
    _say("CAPABILITIES THE AGENTS WILL SEE THIS RUN")
    _say(f"(discovered from {settings.resolve_path('hardware.controls_config', '../config/controls.yaml')})")
    _say("=" * 68)
    _say(caps.summary_for_prompt())
    _say("")
    _say(f"LLM profile per agent (from config/agentic.yaml):")
    for agent in ("device", "scenario", "planner", "executor", "evaluator",
                  "rca", "reporter", "observer"):
        profile = ctx.llm.profile(agent)
        _say(f"  {agent:<10} -> {profile.get('_name')} "
             f"({profile.get('provider')}/{profile.get('model')})")
    if ctx.llm.errors:
        _say("")
        _say("LLM PROBLEMS (the run would fall back to deterministic logic):")
        for err in ctx.llm.errors:
            _say(f"  - {err}")
    return 0


def _collect_scenarios(target: str) -> list[Path]:
    path = Path(target)
    if path.is_dir():
        return sorted(p for p in path.iterdir()
                      if p.suffix.lower() in SCENARIO_SUFFIXES)
    return [path] if path.is_file() else []


# ==========================================================================
# Reporting to the console
# ==========================================================================
def _print_result(state: dict) -> None:
    report = state.get("report")
    verdict = state.get("verdict", Verdict.INCONCLUSIVE)
    label = getattr(verdict, "value", str(verdict)).upper()

    _say("")
    _say("=" * 68)
    _say(f"VERDICT: {label}")
    _say("=" * 68)

    if report is not None and report.executive_summary:
        _say(report.executive_summary)
        _say("")

    evaluation = state.get("evaluation")
    if evaluation is not None and evaluation.criteria:
        _say("CRITERIA")
        for crit in evaluation.criteria:
            word = {True: "  met     ", False: "  NOT met ",
                    None: "  unknown "}[crit.met]
            _say(f"{word} [{crit.criterion_id}] {crit.statement}")
        _say("")

    silent = [r.step.id for r in state.get("step_results", [])
              if r.silent_failure]
    if silent:
        _say(f"SILENT FAILURES at {', '.join(silent)}: the firmware accepted the")
        _say("command and the screen did not change. Input is not reaching")
        _say("xCloud - a firmware OK would have called this a pass.")
        _say("")

    rca = state.get("root_cause")
    if rca is not None:
        _say(f"ROOT CAUSE ({rca.layer} / {rca.primary.cause_class.value})")
        _say(f"  {rca.primary.statement}")
        if rca.primary.discriminating_test:
            _say(f"  Check that would disprove this: "
                 f"{rca.primary.discriminating_test}")
        _say("")

    if report is not None and report.recommendations:
        _say("RECOMMENDATIONS")
        for index, rec in enumerate(report.recommendations, start=1):
            _say(f"  {index}. {rec}")
        _say("")

    if state.get("halt_reason"):
        _say(f"HALTED: {state['halt_reason']}")
        _say("")

    for err in state.get("errors", [])[:3]:
        _say(f"ERROR: {err.splitlines()[0]}")


# ==========================================================================
# main
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentic",
        description="Multi-agent tester for Xbox Cloud Gaming (a PWA) on a "
                    "physical Android phone, driven through an Arduino HID pad.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python -m agentic "open xCloud and check the controller is detected"
  python -m agentic --scenario scenarios/controller_detected.yaml
  python -m agentic --scenario scenarios/ --all
  python -m agentic --check
  python -m agentic --capabilities
  python -m agentic --dry-run --scenario scenarios/navigate_library.yaml

exit codes: 0 pass, 1 fail, 2 blocked, 3 inconclusive, 4 error
""")

    parser.add_argument("scenario_text", nargs="?", default=None,
                        help="the scenario in plain words, or a path to a file")
    parser.add_argument("--scenario", "-s", default=None,
                        help="scenario file, or a directory with --all")
    parser.add_argument("--all", action="store_true",
                        help="run every scenario in the directory")
    parser.add_argument("--config", default=None,
                        help="path to agentic.yaml (default: config/agentic.yaml)")

    parser.add_argument("--check", action="store_true",
                        help="probe the rig and exit; sends no input")
    parser.add_argument("--capabilities", action="store_true",
                        help="print what the agents will be allowed to do")

    parser.add_argument("--dry-run", action="store_true",
                        help="plan and reason, but never open the serial port")
    parser.add_argument("--mode", choices=["plan", "reactive", "adaptive"],
                        default=None, help="execution strategy")
    parser.add_argument("--port", default=None,
                        help="serial port, e.g. COM8 (default: auto-detect)")
    parser.add_argument("--transport", default=None,
                        help="transport profile from controls.yaml")
    parser.add_argument("--device", default=None,
                        help="adb device serial, when several are attached")
    parser.add_argument("--llm", default=None,
                        help="LLM profile name from config/agentic.yaml")
    parser.add_argument("--no-vision", action="store_true",
                        help="disable screenshot analysis (verdicts will be "
                             "capped at inconclusive)")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--max-replans", type=int, default=None)
    parser.add_argument("--quiet", "-q", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings(args.config)

    # Flags -> config keys. One resolution path, no parallel universe of state.
    if args.dry_run:
        settings.override("hardware.dry_run", True)
    if args.mode:
        settings.override("execution.mode", args.mode)
    if args.port:
        settings.override("hardware.serial_port", args.port)
    if args.transport:
        settings.override("hardware.transport", args.transport)
    if args.device:
        settings.override("android.serial", args.device)
    if args.llm:
        settings.override("llm.active", args.llm)
    if args.no_vision:
        settings.override("vision.enabled", False)
        settings.override("vision.llm_screen_reading", False)
    if args.max_steps is not None:
        settings.override("execution.max_steps", args.max_steps)
    if args.max_replans is not None:
        settings.override("retry.max_replans", args.max_replans)

    if not settings.config_found:
        _say(f"NOTE: {settings.path} was not found, so built-in defaults are in "
             f"use. Every setting has a documented default, so this still runs.")

    if args.check:
        return cmd_check(settings)
    if args.capabilities:
        return cmd_capabilities(settings)

    # -- resolve the scenario(s) ---------------------------------------
    target = args.scenario or args.scenario_text
    if not target:
        _say("No scenario given.\n")
        _say("Describe what to test in plain words, for example:\n")
        _say('  python -m agentic "open xCloud and confirm the controller is '
             'detected"\n')
        _say("or point at a file:\n")
        _say("  python -m agentic --scenario scenarios/controller_detected.yaml")
        _say("\nTo see what the rig can do first:  python -m agentic --check")
        return 4

    if args.all:
        paths = _collect_scenarios(target)
        if not paths:
            _say(f"no scenario files ({', '.join(SCENARIO_SUFFIXES)}) found in "
                 f"{target}")
            return 4
        worst = 0
        results: list[tuple[str, str]] = []
        for path in paths:
            _say("")
            _say("#" * 68)
            _say(f"# {path.name}")
            _say("#" * 68)
            text, source = ScenarioAgent.load_raw(str(path))
            state = run_test(text, settings, source,
                             progress=None if args.quiet else _say)
            _print_result(state)
            verdict = state.get("verdict", Verdict.INCONCLUSIVE)
            code = EXIT_CODES.get(verdict, 4)
            results.append((path.name,
                            getattr(verdict, "value", str(verdict))))
            worst = max(worst, code)
        _say("")
        _say("=" * 68)
        _say("SUITE SUMMARY")
        _say("=" * 68)
        for name, verdict in results:
            _say(f"  {verdict.upper():<14} {name}")
        return worst

    text, source = ScenarioAgent.load_raw(str(target))
    state = run_test(text, settings, source,
                     progress=None if args.quiet else _say)
    _print_result(state)

    report = state.get("report")
    if report is not None:
        _say(f"Reports written to {settings.report_dir()}")

    return EXIT_CODES.get(state.get("verdict", Verdict.INCONCLUSIVE), 4)


if __name__ == "__main__":
    sys.exit(main())
