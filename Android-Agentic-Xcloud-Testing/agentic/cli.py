"""
cli.py - argument parsing, run orchestration, and console output.

    python main.py --list                      every suite and case
    python main.py --check                     probe the rig, send nothing
    python main.py --capabilities              what the agents will be allowed
    python main.py --case controller_detected  one case
    python main.py --suite smoke               a group, in a deliberate order
    python main.py --tag slow                  everything carrying a tag
    python main.py "press A and watch"         inline prose, no file needed

Every flag maps to a dotted config key via `Settings.override`, so there is
exactly ONE resolution path (flag > env > yaml > default) and no flag can quietly
mean something different from its config key.

The exit code IS the verdict, for CI:
    0 pass   1 fail   2 blocked   3 inconclusive   4 error
`inconclusive` is deliberately not 0. A run that proved nothing must not be able
to turn a pipeline green - that is the whole discipline of this project applied
to its own exit status.
"""

from __future__ import annotations

import argparse
import sys

from .graph import build_context, run_test
from .schemas import Verdict
from .settings import Settings
from .suites import SuiteLoader, TestCase

EXIT_CODES = {
    Verdict.PASS: 0,
    Verdict.FAIL: 1,
    Verdict.BLOCKED: 2,
    Verdict.INCONCLUSIVE: 3,
    Verdict.ERROR: 4,
}

# Ranked worst-first, for deciding a suite's overall result. FAIL outranks
# BLOCKED because a real failure is the more actionable finding.
SEVERITY = {Verdict.PASS: 0, Verdict.INCONCLUSIVE: 1, Verdict.BLOCKED: 2,
            Verdict.FAIL: 3, Verdict.ERROR: 4}

BAR = "=" * 72
RULE = "-" * 72


def _say(message: str = "") -> None:
    """Print with an ASCII-safe fallback.

    The Windows console is cp1252 by default, and a stray character in a
    model's prose would otherwise raise UnicodeEncodeError - crashing the tool
    over a display concern, exactly the trap pad_link.py documents for corrupted
    serial bytes.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"))


# ==========================================================================
# Commands that run no test
# ==========================================================================
def cmd_list(loader: SuiteLoader) -> int:
    _say(BAR)
    _say("AVAILABLE TESTS")
    _say(BAR)
    _say(loader.describe())
    _say()
    _say("Scenarios are free text - YAML, markdown, or a sentence on the")
    _say("command line. There is no schema to learn; the agents read them.")
    return 0


def cmd_check(settings: Settings) -> int:
    """Probe the rig. Sends no input and touches no scenario."""
    from .agents import DeviceAgent
    from .state import new_state

    ctx = build_context(settings, "check")
    agent = DeviceAgent(ctx)
    try:
        result = agent.run(new_state("", "<check>"))
    finally:
        ctx.pad.close()

    env = result["environment"]
    _say(BAR)
    _say("RIG CHECK")
    _say(BAR)
    _say(agent._facts(env))                          # noqa: SLF001
    _say()
    _say("ASSESSMENT")
    _say(env.assessment or "(none available)")
    _say()

    if env.ready:
        _say("READY. Note what this does and does not prove: the BOARD answers,")
        _say("and a HOST has enumerated the pad. It does NOT prove xCloud")
        _say("reacts - only observing the screen can show that.")
        return 0

    _say("NOT READY:")
    for reason in env.blocking_reasons:
        _say(f"  - {reason}")
    return 2


def cmd_capabilities(settings: Settings) -> int:
    """Print exactly what the agents will be told they can do."""
    from .agents import DeviceAgent
    from .state import new_state

    ctx = build_context(settings, "capabilities")
    try:
        result = DeviceAgent(ctx).run(new_state("", "<capabilities>"))
    finally:
        ctx.pad.close()

    controls = settings.resolve_path("hardware.controls_config",
                                     "../config/controls.yaml")
    _say(BAR)
    _say("CAPABILITIES THE AGENTS WILL SEE")
    _say(f"discovered at runtime from {controls}")
    _say(BAR)
    _say(result["capabilities"].summary_for_prompt())
    _say()
    _say("LLM PROFILE PER AGENT (config/agentic.yaml)")
    for agent in ("device", "scenario", "planner", "executor", "evaluator",
                  "rca", "reporter", "observer"):
        profile = ctx.llm.profile(agent)
        _say(f"  {agent:<10} {profile.get('_name'):<16} "
             f"{profile.get('provider')}/{profile.get('model')}")
    if ctx.llm.errors:
        _say()
        _say("LLM PROBLEMS - the run would fall back to deterministic logic:")
        for err in ctx.llm.errors:
            _say(f"  - {err}")
    return 0


# ==========================================================================
# Console report for a single run
# ==========================================================================
def _print_result(state: dict) -> None:
    report = state.get("report")
    verdict = state.get("verdict", Verdict.INCONCLUSIVE)
    label = getattr(verdict, "value", str(verdict)).upper()

    _say()
    _say(BAR)
    _say(f"  VERDICT: {label}")
    _say(BAR)

    evaluation = state.get("evaluation")
    if evaluation is not None and evaluation.criteria:
        met = sum(1 for c in evaluation.criteria if c.met is True)
        no = sum(1 for c in evaluation.criteria if c.met is False)
        unknown = sum(1 for c in evaluation.criteria if c.met is None)
        _say(f"  {met} met | {no} not met | {unknown} unverified"
             f"   (confidence {evaluation.confidence:.0%})")
        _say()
        for crit in evaluation.criteria:
            mark = {True: "[ met ]", False: "[ FAIL]",
                    None: "[  ?  ]"}[crit.met]
            _say(f"  {mark} {crit.criterion_id}: {crit.statement}")
            if crit.met is not True and crit.reasoning:
                _say(f"          {crit.reasoning[:150]}")
        _say()

    results = state.get("step_results", [])
    if results:
        _say(RULE)
        _say("  STEPS")
        _say(RULE)
        for r in results:
            mark = {True: " ok ", False: "FAIL", None: " ?  "}[r.expectation_met]
            action = f"{r.step.kind.value} {r.step.target or ''}".strip()
            flag = "  <<< SILENT FAILURE" if r.silent_failure else ""
            _say(f"  [{mark}] {r.step.id:<32} {action}{flag}")
        _say()

    silent = [r.step.id for r in results if r.silent_failure]
    if silent:
        _say(RULE)
        _say("  SILENT FAILURE - the finding a firmware OK cannot give you")
        _say(RULE)
        _say(f"  At: {', '.join(silent)}")
        _say("  The firmware accepted the command and the screen did not")
        _say("  change. Input is not reaching xCloud.")
        _say()

    rca = state.get("root_cause")
    if rca is not None:
        _say(RULE)
        _say(f"  ROOT CAUSE - layer: {rca.layer} / "
             f"{rca.primary.cause_class.value}")
        _say(RULE)
        _say(f"  {rca.primary.statement}")
        if rca.primary.discriminating_test:
            _say()
            _say("  Check that could DISPROVE this:")
            _say(f"  {rca.primary.discriminating_test}")
        _say()

    if report is not None and report.executive_summary:
        _say(RULE)
        _say("  SUMMARY")
        _say(RULE)
        for line in report.executive_summary.splitlines():
            _say(f"  {line}")
        _say()

    if report is not None and report.recommendations:
        _say(RULE)
        _say("  NEXT ACTIONS")
        _say(RULE)
        for index, rec in enumerate(report.recommendations, start=1):
            _say(f"  {index}. {rec}")
        _say()

    if state.get("halt_reason"):
        _say(f"  HALTED: {state['halt_reason']}")
        _say()
    for err in state.get("errors", [])[:3]:
        _say(f"  ERROR: {err.splitlines()[0]}")


def _print_suite_summary(results: list[tuple[str, Verdict]]) -> None:
    _say()
    _say(BAR)
    _say("  SUITE SUMMARY")
    _say(BAR)
    for name, verdict in results:
        label = getattr(verdict, "value", str(verdict)).upper()
        _say(f"  {label:<14} {name}")
    tally: dict[str, int] = {}
    for _, verdict in results:
        key = getattr(verdict, "value", str(verdict))
        tally[key] = tally.get(key, 0) + 1
    _say(RULE)
    _say("  " + " | ".join(f"{v} {k}" for k, v in sorted(tally.items())))


# ==========================================================================
# Running
# ==========================================================================
def _run_case(case: TestCase, settings: Settings, quiet: bool) -> Verdict:
    state = run_test(case.text, settings, str(case.path),
                     progress=None if quiet else _say)
    _print_result(state)
    return state.get("verdict", Verdict.INCONCLUSIVE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Multi-agent tester for Xbox Cloud Gaming (a PWA) on a "
                    "physical Android phone, driven by an Arduino HID gamepad.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python main.py --list
  python main.py --check
  python main.py --suite smoke
  python main.py --case controller_detected
  python main.py --tag slow
  python main.py "open xCloud and confirm the controller is detected"
  python main.py --dry-run --case stream_launch

exit codes: 0 pass, 1 fail, 2 blocked, 3 inconclusive, 4 error
            (inconclusive is NOT 0 - a run that proved nothing must not pass CI)
""")

    what = parser.add_argument_group("what to run")
    what.add_argument("scenario_text", nargs="?", default=None,
                      help="the scenario in plain words, or a path to a file")
    what.add_argument("--case", "-c", default=None,
                      help="one test case, by id or filename")
    what.add_argument("--suite", "-s", default=None,
                      help="a named suite from scenarios/suites.yaml")
    what.add_argument("--tag", "-t", default=None,
                      help="every case carrying this tag")
    what.add_argument("--scenario", default=None,
                      help="an explicit scenario file path")
    what.add_argument("--all", action="store_true",
                      help="every case found under scenarios/")

    info = parser.add_argument_group("inspect, without testing")
    info.add_argument("--list", "-l", action="store_true",
                      help="show every suite and case, then exit")
    info.add_argument("--check", action="store_true",
                      help="probe the rig and exit; sends no input")
    info.add_argument("--capabilities", action="store_true",
                      help="print what the agents will be allowed to do")

    how = parser.add_argument_group("how to run")
    how.add_argument("--dry-run", action="store_true",
                     help="plan and reason, but never open the serial port")
    how.add_argument("--mode", choices=["plan", "reactive", "adaptive"],
                     default=None, help="execution strategy")
    how.add_argument("--port", default=None, help="serial port, e.g. COM8")
    how.add_argument("--transport", default=None,
                     help="transport profile from controls.yaml")
    how.add_argument("--device", default=None, help="adb device serial")
    how.add_argument("--llm", default=None,
                     help="LLM profile name from config/agentic.yaml")
    how.add_argument("--no-vision", action="store_true",
                     help="disable screenshot analysis (caps verdicts at "
                          "inconclusive)")
    how.add_argument("--max-steps", type=int, default=None)
    how.add_argument("--max-replans", type=int, default=None)
    how.add_argument("--config", default=None, help="path to agentic.yaml")
    how.add_argument("--continue-on-failure", action="store_true",
                     help="override a suite's stop_on_failure")
    how.add_argument("--quiet", "-q", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings(args.config)
    loader = SuiteLoader()

    # Flags -> config keys. One resolution path; no parallel universe of state.
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
        _say(f"NOTE: {settings.path} not found - built-in defaults are in use. "
             f"Every setting has a documented default, so this still runs.")

    # -- inspection --------------------------------------------------------
    if args.list:
        return cmd_list(loader)
    if args.check:
        return cmd_check(settings)
    if args.capabilities:
        return cmd_capabilities(settings)

    # -- resolve what to run ----------------------------------------------
    cases: list[TestCase] = []
    stop_on_failure = False
    heading = ""

    if args.suite:
        suite = loader.load_suite(args.suite)
        if suite is None:
            available = ", ".join(loader.suite_definitions()) or "none defined"
            _say(f"unknown suite '{args.suite}'. Available: {available}")
            return 4
        cases = suite.cases
        stop_on_failure = suite.stop_on_failure and not args.continue_on_failure
        heading = f"SUITE: {suite.name} - {suite.description.splitlines()[0] if suite.description else ''}"
        for miss in suite.missing:
            _say(f"WARNING: suite '{suite.name}' references a missing case: "
                 f"{miss}")

    elif args.tag:
        cases = loader.find_by_tag(args.tag)
        if not cases:
            _say(f"no cases carry the tag '{args.tag}'. Try --list.")
            return 4
        heading = f"TAG: {args.tag}"

    elif args.case:
        found = loader.find_case(args.case)
        if found is None:
            _say(f"unknown case '{args.case}'. Try --list.")
            return 4
        cases = [found]

    elif args.all:
        cases = loader.all_cases()
        if not cases:
            _say("no cases found under scenarios/.")
            return 4
        heading = "ALL CASES"

    elif args.scenario or args.scenario_text:
        target = args.scenario or args.scenario_text
        found = loader.find_case(str(target))
        if found is not None:
            cases = [found]
        else:
            # Inline prose. The point of the whole system: no file required.
            cases = [TestCase(id="inline", title=str(target)[:60],
                              path=__import__("pathlib").Path("<inline>"),
                              text=str(target))]

    else:
        _say("Nothing to run.")
        _say()
        _say("  python main.py --list                     see what exists")
        _say("  python main.py --check                    is the rig ready?")
        _say("  python main.py --suite smoke              run the quick suite")
        _say('  python main.py "check the A button works" describe it yourself')
        return 4

    # -- run ---------------------------------------------------------------
    if heading:
        _say(BAR)
        _say(f"  {heading}")
        _say(f"  {len(cases)} case(s)"
             + ("  (stops at the first failure)" if stop_on_failure else ""))
        _say(BAR)

    single = len(cases) == 1
    results: list[tuple[str, Verdict]] = []
    worst = Verdict.PASS

    for index, case in enumerate(cases, start=1):
        if not single:
            _say()
            _say("#" * 72)
            _say(f"#  [{index}/{len(cases)}] {case.name} - {case.title}")
            _say("#" * 72)

        verdict = _run_case(case, settings, args.quiet)
        results.append((case.name, verdict))
        if SEVERITY.get(verdict, 4) > SEVERITY.get(worst, 0):
            worst = verdict

        if stop_on_failure and verdict in (Verdict.FAIL, Verdict.BLOCKED,
                                           Verdict.ERROR):
            # Everything after this would most likely fail for the SAME reason,
            # which buries the real finding under repetition.
            _say()
            _say(f"STOPPING: '{case.name}' returned {verdict.value}, and this "
                 f"suite stops on failure.")
            remaining = len(cases) - index
            if remaining:
                _say(f"{remaining} case(s) were not run. Fix this one first, or "
                     f"pass --continue-on-failure to run them anyway.")
            break

    if not single:
        _print_suite_summary(results)

    _say()
    _say(f"Reports: {settings.report_dir()}")
    return EXIT_CODES.get(worst, 4)


if __name__ == "__main__":
    sys.exit(main())
