"""
test-controller.py - exercise EVERY control on the pad, one at a time, and
                     record whether the phone actually reacted.

    python host/test-controller.py                 walk every control, ask each time
    python host/test-controller.py --auto          send everything, ask nothing
    python host/test-controller.py --only a b up   just these
    python host/test-controller.py --buttons       skip triggers and sticks
    python host/test-controller.py --quick         one pass, short waits, no prompts
    python host/test-controller.py --repeat 3      send each control 3 times
    python host/test-controller.py --list          show what would be tested
    python host/test-controller.py --dry-run       print commands, touch no hardware
    python host/test-controller.py --report r.md   write a Markdown result table

WHY THIS EXISTS WHEN pad_link.py ALREADY HAS `macro hid_selftest`
----------------------------------------------------------------
`hid_selftest` fires all 18 controls in a few seconds and prints `ok` after each.
That is useful, and it is also the exact trap this project keeps re-learning:

    OK means the FIRMWARE QUEUED AN HID REPORT.
    It does not mean the phone received it.
    It certainly does not mean xCloud reacted.

So a run of `hid_selftest` that prints eighteen `ok` lines is compatible with a
phone that is not even plugged in - the board queues reports to a host that
isn't listening and reports success every time. 3-TEST.bat papers over this by
asking "Did the phone react?" ONCE, at the end, about all eighteen controls at
once. If the answer is no you learn nothing about WHICH control failed, and if
only `guide` is broken (which controls.yaml explicitly flags as unverified on
Android) you will most likely answer "yes" and never find out.

This script closes that gap. It goes one control at a time, pauses, and asks
what you SAW. That makes the human the sensor - the only sensor available on
this rig without adb - and turns eighteen unfalsifiable `ok`s into a per-control
pass/fail table you can act on.

WHAT EACH VERDICT MEANS, precisely
----------------------------------
    OK          firmware queued it AND you confirmed the phone reacted.
                This is the only outcome that proves the whole chain.
    NO REACT    firmware queued it and you saw NOTHING. This is the valuable
                finding: a silent failure, localised to one control.
    SEND FAIL   the firmware itself refused or did not answer. A wiring,
                port or naming fault - not an Android problem. The reason is
                printed verbatim from the board.
    UNSURE      you could not tell. Recorded as unproven, never as a pass,
                because a test that cannot say "no" is worth nothing.
    SKIPPED     not tested this run.

HOW TO WATCH
------------
Have ONE of these in front of you, in order of usefulness:

  1. A "Gamepad Tester" app on the phone. Best by far: each button lights up
     individually and the sticks/triggers show numeric values, so you can tell
     `lb` from `rb` and see a trigger's analog range rather than guessing.
  2. xCloud itself. Realistic, but a poor instrument for this job - it only
     visibly responds to D-pad and A/B, so `x`, `y`, `ls`, `rs` and the
     triggers will look dead even when they are working perfectly.
  3. Nothing. Then use --auto and understand that you are only testing the
     serial link and the firmware, which is a much weaker claim.

Everything comes from ../config/controls.yaml - buttons, triggers, sticks,
macros and timing. Add a control to that file and it is tested on the next run
with no change here, which is the same rule pad_link.py follows.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pad_link import AndroidPad, ControlConfig, PadLink  # noqa: E402

BAR = "=" * 70
RULE = "-" * 70

# The four face buttons, for --faces. Named here rather than inline so the CLI
# help and the filter can never disagree about what "faces" means.
FACE_BUTTONS = ("a", "b", "x", "y")


# Verdicts. Ordered worst-last so a summary can sort by severity.
OK = "OK"
NO_REACT = "NO REACT"
SEND_FAIL = "SEND FAIL"
UNSURE = "UNSURE"
SKIPPED = "SKIPPED"


def say(message: str = "") -> None:
    """Print with an ASCII-safe fallback.

    The Windows console is cp1252. pad_link.py documents a real case where a
    corrupted UART byte decoded to an unprintable character and crashed the tool
    with UnicodeEncodeError - losing the run over a DISPLAY concern rather than
    reporting the actual fault. Same guard here.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"))


# ==========================================================================
# What to test
# ==========================================================================
class Control:
    """One testable thing, plus how to send it and what to expect on screen.

    `expect` is written for a HUMAN to check, and is deliberately specific.
    "Something happens" is not checkable; "the highlight moves one tile right"
    is. Vague expectations are how a broken control gets waved through.
    """

    def __init__(self, name: str, kind: str, expect: str,
                 send: Any, detail: str = ""):
        self.name = name
        self.kind = kind          # button | dpad | trigger | stick | macro
        self.expect = expect
        self.send = send          # callable(pad) -> bool
        self.detail = detail
        self.verdict = SKIPPED
        self.note = ""

    def __repr__(self) -> str:
        return f"<Control {self.name} {self.kind}>"


def build_controls(cfg: ControlConfig, args: argparse.Namespace) -> list[Control]:
    """Derive the test list from controls.yaml. Nothing is hardcoded.

    Order matters. The D-pad goes FIRST because it is the one group that is
    visibly testable in xCloud itself, so a user without a gamepad-tester app
    gets a meaningful answer before hitting the controls they cannot observe.
    """
    controls: list[Control] = []
    hold = cfg.timing_value("press_duration", 0.10)

    # -- D-pad (HID hat) ---------------------------------------------------
    # Separated from ordinary buttons because it is a different HID mechanism
    # (a hat, not a button) and because it is the group most likely to work when
    # everything else appears dead - Android navigation needs the hat.
    hat_expect = {
        "up": "the highlight/selection moves UP one item",
        "down": "the highlight/selection moves DOWN one item",
        "left": "the highlight/selection moves LEFT one item",
        "right": "the highlight/selection moves RIGHT one item",
    }
    for name, spec in cfg.buttons.items():
        if "hat" not in spec:
            continue
        controls.append(Control(
            name=name, kind="dpad",
            expect=hat_expect.get(name,
                                  f"the D-pad {name} direction registers"),
            detail=f"HID hat angle {spec['hat']}",
            send=lambda pad, n=name: pad.press(n, hold)))

    # -- ordinary buttons --------------------------------------------------
    # The expectations here are honest about xCloud's limits: several of these
    # do nothing visible in a menu, and saying so up front stops a working
    # control being marked broken.
    btn_expect = {
        "a": "the focused item is SELECTED / opened (A = confirm)",
        "b": "the view goes BACK one level (B = cancel)",
        "x": "usually NOTHING VISIBLE in an xCloud menu - use a gamepad "
             "tester to see X light up",
        "y": "usually NOTHING VISIBLE in an xCloud menu - use a gamepad "
             "tester to see Y light up",
        "menu": "a menu/pause overlay opens, or nothing in a plain web page",
        "view": "usually nothing visible in xCloud; lights up in a tester",
        "guide": "the Xbox overlay opens IN A STREAM. Outside a stream, or on "
                 "some Android builds, the OS swallows it - controls.yaml "
                 "flags this one as UNVERIFIED, so a failure here is expected "
                 "and is not necessarily a fault",
        "lb": "shoulder button - moves between tabs/rails in some views, "
              "otherwise only visible in a tester",
        "rb": "shoulder button - moves between tabs/rails in some views, "
              "otherwise only visible in a tester",
        "ls": "left stick CLICK - usually only visible in a tester",
        "rs": "right stick CLICK - usually only visible in a tester",
    }
    for name, spec in cfg.buttons.items():
        if "hat" in spec:
            continue
        controls.append(Control(
            name=name, kind="button",
            expect=btn_expect.get(name, f"the {name} button registers"),
            detail=f"HID button '{spec['hid']}'",
            send=lambda pad, n=name: pad.press(n, hold)))

    # -- triggers ----------------------------------------------------------
    # Tested at two values, because a trigger that only ever reports 0 or 255 is
    # a DIGITAL button wearing an analog costume - a real defect that a single
    # full press cannot reveal.
    for name, spec in cfg.triggers.items():
        full = int(spec.get("default_press", 255))
        half = full // 2
        controls.append(Control(
            name=name, kind="trigger",
            expect=f"the {name} bar in a gamepad tester moves to about "
                   f"{half}/{full} (HALF), not straight to maximum. Nothing "
                   f"visible in an xCloud menu",
            detail=f"analog {spec.get('min', 0)}..{full}, sent at {half}",
            send=lambda pad, n=name, v=half: pad.trigger(n, v, 0.35)))
        controls.append(Control(
            name=f"{name} (full)", kind="trigger",
            expect=f"the {name} bar reaches its MAXIMUM ({full})",
            detail=f"analog full scale {full}",
            send=lambda pad, n=name, v=full: pad.trigger(n, v, 0.35)))

    # -- sticks ------------------------------------------------------------
    for stick_name, spec in cfg.sticks.items():
        for direction in (spec.get("directions") or {}):
            controls.append(Control(
                name=f"{stick_name}:{direction}", kind="stick",
                expect=f"the {stick_name.replace('_', ' ')} deflects "
                       f"{direction.upper()} and returns to centre",
                detail=f"axes {spec.get('x_axis')}/{spec.get('y_axis')}",
                send=lambda pad, s=stick_name, d=direction:
                    pad.stick(s, d, duration=0.45)))

    # -- filtering ---------------------------------------------------------
    if args.buttons:
        controls = [c for c in controls if c.kind in ("button", "dpad")]
    if args.dpad:
        controls = [c for c in controls if c.kind == "dpad"]
    if args.sticks:
        controls = [c for c in controls if c.kind == "stick"]
    if args.triggers:
        controls = [c for c in controls if c.kind == "trigger"]
    if args.faces:
        # A/B/X/Y. Worth a shortcut of its own because it is the group people
        # actually reach for: A and B are the only two buttons xCloud visibly
        # reacts to, so pairing them with X and Y in one short run answers
        # "are the face buttons wired correctly" faster than anything else.
        controls = [c for c in controls
                    if c.kind == "button" and c.name in FACE_BUTTONS]


    if args.only:
        wanted = {w.strip().lower() for w in args.only}
        kept = [c for c in controls
                if c.name.lower() in wanted
                or c.name.split(":")[0].lower() in wanted
                or c.name.split(" ")[0].lower() in wanted]
        unknown = wanted - {c.name.lower() for c in kept} \
                         - {c.name.split(":")[0].lower() for c in kept} \
                         - {c.name.split(" ")[0].lower() for c in kept}
        if unknown:
            # Named but not found: say so rather than silently testing less than
            # was asked for. A typo that quietly reduces coverage is worse than
            # an error, because the run still looks like it passed.
            say(f"WARNING: not in controls.yaml, so not tested: "
                f"{', '.join(sorted(unknown))}")
        controls = kept

    return controls


# ==========================================================================
# Asking the human
# ==========================================================================
def ask_reaction(control: Control) -> tuple[str, str]:
    """Ask what was actually seen. Returns (verdict, note).

    The default is deliberately NOT "yes". Pressing Enter to move on quickly is
    the natural impulse, and if Enter meant "it worked" this whole script would
    become a machine for manufacturing false passes. Enter means "I could not
    tell", which is honest and is not a pass.
    """
    prompt = ("      did it react?  [y]es  [n]o  [s]kip  [q]uit  "
              "(Enter = not sure): ")
    while True:
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "QUIT", "interrupted by the user"

        if answer in ("y", "yes"):
            return OK, ""
        if answer in ("n", "no"):
            note = input("      what DID happen (Enter for 'nothing'): ").strip()
            return NO_REACT, note or "nothing visible happened"
        if answer in ("s", "skip"):
            return SKIPPED, "skipped by the user"
        if answer in ("q", "quit"):
            return "QUIT", "stopped early by the user"
        if answer == "":
            return UNSURE, "the user could not tell"
        say("      please answer y, n, s, q, or press Enter for 'not sure'.")


# ==========================================================================
# The run
# ==========================================================================
def run_controls(pad: AndroidPad, controls: list[Control],
                 args: argparse.Namespace) -> bool:
    """Send each control and collect a verdict. Returns False if stopped early."""
    total = len(controls)
    for index, control in enumerate(controls, start=1):
        say()
        say(RULE)
        say(f"  [{index}/{total}]  {control.name}"
            + (f"   ({control.detail})" if control.detail else ""))
        say(RULE)
        say(f"  EXPECT: {control.expect}")

        sent_ok = True
        for attempt in range(max(1, args.repeat)):
            if args.repeat > 1:
                say(f"  send {attempt + 1}/{args.repeat}")
            if not control.send(pad):
                sent_ok = False
                break
            if attempt < args.repeat - 1:
                time.sleep(args.gap)

        if not sent_ok:
            # The firmware refused. That is a different fault from "the phone
            # ignored it", and conflating the two sends someone to debug Android
            # when the problem is a wire. pad_link already printed the reason.
            control.verdict = SEND_FAIL
            control.note = "the board did not accept the command"
            say("  RESULT: SEND FAIL - the firmware refused or did not answer.")
            say("          This is a serial/wiring/firmware fault, NOT an")
            say("          Android one. The board's reason is printed above.")
            if not args.keep_going:
                say()
                say("  Stopping: a send failure usually repeats for every")
                say("  remaining control, and one clear reason beats twenty")
                say("  copies of it. Pass --keep-going to continue anyway.")
                return False
            continue

        if args.auto:
            # Nothing was observed, so nothing may be claimed. Recording this as
            # UNSURE rather than OK is the entire honesty rule of this project
            # applied to its own output.
            control.verdict = UNSURE
            control.note = ("--auto: the command was accepted but nothing was "
                            "observed, so no reaction can be claimed")
            continue

        time.sleep(args.settle)
        verdict, note = ask_reaction(control)
        if verdict == "QUIT":
            control.note = note
            return False
        control.verdict, control.note = verdict, note

    return True


# ==========================================================================
# Reporting
# ==========================================================================
def summarise(controls: list[Control], elapsed: float,
              pad_status: dict[str, Any], args: argparse.Namespace) -> int:
    """Print the table, return a process exit code."""
    tested = [c for c in controls if c.verdict != SKIPPED]
    passed = [c for c in controls if c.verdict == OK]
    dead = [c for c in controls if c.verdict == NO_REACT]
    failed = [c for c in controls if c.verdict == SEND_FAIL]
    unsure = [c for c in controls if c.verdict == UNSURE]
    skipped = [c for c in controls if c.verdict == SKIPPED]

    say()
    say(BAR)
    say("  RESULTS")
    say(BAR)
    for control in controls:
        mark = {OK: "[ ok ]", NO_REACT: "[FAIL]", SEND_FAIL: "[SEND]",
                UNSURE: "[ ?  ]", SKIPPED: "[skip]"}[control.verdict]
        line = f"  {mark} {control.name:<24} {control.kind:<8}"
        if control.note:
            line += f" {control.note[:60]}"
        say(line)

    say(RULE)
    say(f"  {len(passed)} reacted | {len(dead)} did NOT react | "
        f"{len(failed)} send failures | {len(unsure)} unproven | "
        f"{len(skipped)} skipped     ({elapsed:.0f}s)")
    say()

    # -- what the run actually established --------------------------------
    say(BAR)
    say("  WHAT THIS PROVES")
    say(BAR)
    if failed:
        say("  The board REFUSED some commands. Nothing can be concluded about")
        say("  Android until that is fixed - the reports never left the PC.")
        say(f"    affected: {', '.join(c.name for c in failed)}")
    elif not tested:
        say("  Nothing was tested.")
    elif args.auto:
        say("  --auto was used, so every control was SENT and none was")
        say("  OBSERVED. This proves the serial link and the firmware accept")
        say("  commands. It proves NOTHING about the phone or xCloud: the")
        say("  board queues reports to a host whether or not one is listening,")
        say("  and reports OK either way. Re-run without --auto to learn more.")
    elif dead:
        say(f"  {len(dead)} control(s) were accepted by the firmware and did")
        say("  NOT visibly react. That is a SILENT FAILURE and it is the most")
        say("  useful thing this script can find:")
        for control in dead:
            say(f"    - {control.name}: {control.note}")
        say()
        say("  Before calling these broken, rule out the instrument:")
        say("    * x, y, view, ls, rs and the triggers do nothing visible in an")
        say("      xCloud MENU even when perfect. Re-test them in a gamepad")
        say("      tester app before believing a failure.")
        say("    * `guide` is flagged UNVERIFIED in controls.yaml - some")
        say("      Android builds swallow the HID Home usage entirely.")
        say("    * if the D-pad works and nothing else does, the pad IS")
        say("      connected and the fault is per-button, not the link.")
        say("    * if NOTHING reacted, suspect the link instead: the ON LED")
        say("      unlit, an OTG adapter at the wrong end, or a charge-only")
        say("      cable. Run 2-CHECK.bat.")
    elif unsure and not passed:
        say("  Every control was sent and none could be confirmed. This run is")
        say("  INCONCLUSIVE, not a pass. Use a gamepad tester app so the")
        say("  answer is visible, then run again.")
    else:
        say(f"  {len(passed)} of {len(tested)} tested control(s) were confirmed")
        say("  end to end: PC -> serial -> firmware -> HID -> phone -> app.")
        if unsure:
            say(f"  {len(unsure)} could not be judged and are NOT counted as")
            say("  passing: " + ", ".join(c.name for c in unsure))
        if pad_status.get("pad_connected") is False:
            say()
            say("  ODD: the board reports that no USB host has enumerated the")
            say("  pad, yet you saw reactions. Trust what you saw, but the")
            say("  handshake disagreeing is worth a second look.")

    say()
    if args.report:
        write_report(controls, elapsed, pad_status, args)

    # Exit code, for scripting. A silent failure and a send failure are
    # different problems and get different codes.
    if failed:
        return 3
    if dead:
        return 1
    if not passed:
        return 2          # nothing proven - deliberately not 0
    return 0


def write_report(controls: list[Control], elapsed: float,
                 pad_status: dict[str, Any], args: argparse.Namespace) -> None:
    """Write a Markdown table. Small enough to paste into an issue."""
    path = Path(args.report)
    rows = [
        f"# Controller test - {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- duration: {elapsed:.0f}s",
        f"- port: `{pad_status.get('port')}`",
        f"- firmware: `{pad_status.get('firmware')}`",
        f"- transport: `{pad_status.get('transport')}`",
        f"- a USB host had enumerated the pad: "
        f"**{pad_status.get('pad_connected')}**",
        f"- mode: {'auto (nothing observed)' if args.auto else 'confirmed by a human'}",
        "",
        "| Control | Kind | Result | Note |",
        "|---|---|---|---|",
    ]
    for c in controls:
        rows.append(f"| `{c.name}` | {c.kind} | {c.verdict} | "
                    f"{c.note.replace('|', '/')} |")
    rows += [
        "",
        "## Reading this",
        "",
        "- **OK** - the firmware queued the report AND a human confirmed the",
        "  phone reacted. The only result that proves the whole chain.",
        "- **NO REACT** - queued and nothing happened. A silent failure,",
        "  localised to one control.",
        "- **SEND FAIL** - the board refused. A serial/wiring/firmware fault,",
        "  not an Android one.",
        "- **UNSURE** - not judgeable. Explicitly not a pass.",
        "",
        "Note that `x`, `y`, `view`, `ls`, `rs` and the triggers produce no",
        "visible change in an xCloud menu even when working correctly, and",
        "`guide` is flagged UNVERIFIED in controls.yaml. Confirm those in a",
        "gamepad-tester app before treating a failure as real.",
    ]
    try:
        path.write_text("\n".join(rows), encoding="utf-8")
        say(f"  Report written to {path}")
    except OSError as exc:
        say(f"  Could not write {path}: {exc}")


# ==========================================================================
# CLI
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python host/test-controller.py",
        description="Exercise every control on the Arduino HID pad and record "
                    "whether the phone actually reacted.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python host/test-controller.py                    every control, asking each time
  python host/test-controller.py --faces            just A, B, X, Y
  python host/test-controller.py --dpad             just the D-pad (most visible)
  python host/test-controller.py --only a b x y     the same four, named explicitly
  python host/test-controller.py --only lt rt       just the triggers
  python host/test-controller.py --auto             send all, ask nothing
  python host/test-controller.py --quick --auto     fastest possible sanity check
  python host/test-controller.py --report out.md    save a result table


exit codes:
  0  every tested control was confirmed working
  1  at least one control was accepted but did NOT react (silent failure)
  2  nothing could be confirmed - inconclusive, deliberately not 0
  3  the board refused a command - a wiring/firmware fault
""")

    what = ap.add_argument_group("what to test")
    what.add_argument("--only", nargs="+", metavar="NAME",
                      help="test only these controls (a, up, lt, left_stick)")
    what.add_argument("--buttons", action="store_true",
                      help="buttons and D-pad only")
    what.add_argument("--faces", action="store_true",
                      help="the four face buttons only: "
                           + ", ".join(FACE_BUTTONS))
    what.add_argument("--dpad", action="store_true", help="D-pad only")

    what.add_argument("--sticks", action="store_true", help="sticks only")
    what.add_argument("--triggers", action="store_true", help="triggers only")
    what.add_argument("--macros", action="store_true",
                      help="also run every macro from controls.yaml at the end")

    how = ap.add_argument_group("how to run")
    how.add_argument("--auto", action="store_true",
                     help="send everything without asking. Records UNSURE, "
                          "never OK - nothing was observed, so nothing is proven")
    how.add_argument("--quick", action="store_true",
                     help="implies --auto with short waits")
    how.add_argument("--repeat", type=int, default=1, metavar="N",
                     help="send each control N times (default 1)")
    how.add_argument("--settle", type=float, default=0.4, metavar="SEC",
                     help="pause after sending, before asking (default 0.4)")
    how.add_argument("--gap", type=float, default=0.35, metavar="SEC",
                     help="pause between repeats (default 0.35)")
    how.add_argument("--keep-going", action="store_true",
                     help="continue after a send failure instead of stopping")

    link = ap.add_argument_group("the link")
    link.add_argument("--port", default=None, help="serial port, e.g. COM8")
    link.add_argument("--transport", default=None,
                      help="transport profile from controls.yaml")
    link.add_argument("--config", default=None, help="path to controls.yaml")
    link.add_argument("--dry-run", action="store_true",
                      help="print commands without opening the port")

    out = ap.add_argument_group("output")
    out.add_argument("--list", action="store_true",
                     help="show what would be tested, then exit")
    out.add_argument("--report", default=None, metavar="FILE",
                     help="write a Markdown result table")
    out.add_argument("--yes", action="store_true",
                     help="skip the 'get ready' prompt")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.quick:
        args.auto = True
        args.settle = 0.15
        args.gap = 0.15

    try:
        cfg = (ControlConfig(args.config) if args.config else ControlConfig())
    except FileNotFoundError as exc:
        say(f"ERROR: {exc}")
        return 3

    controls = build_controls(cfg, args)
    if not controls:
        say("Nothing to test. Check --only against `--list`.")
        return 3

    # -- --list ------------------------------------------------------------
    if args.list:
        say(BAR)
        say(f"  {len(controls)} CONTROL(S) WOULD BE TESTED")
        say(f"  from {cfg.path}")
        say(BAR)
        current = ""
        for c in controls:
            if c.kind != current:
                say(f"\n  {c.kind.upper()}")
                current = c.kind
            say(f"    {c.name:<24} {c.detail}")
            say(f"      expect: {c.expect}")
        if args.macros:
            say(f"\n  MACROS")
            for name, spec in cfg.macros.items():
                say(f"    {name:<24} {spec.get('description', '')}")
        return 0

    # -- brief -------------------------------------------------------------
    say(BAR)
    say("  CONTROLLER TEST")
    say(BAR)
    say(f"  {len(controls)} control(s) from {cfg.path}")
    if args.auto:
        say()
        say("  --auto: every control will be SENT and NONE observed. This")
        say("  checks the serial link and the firmware only. The board queues")
        say("  reports whether or not a host is listening and answers OK")
        say("  either way, so this mode cannot prove the phone received")
        say("  anything. Run without --auto for a real answer.")
    else:
        say()
        say("  You are the sensor. After each control you will be asked what")
        say("  you SAW - because a firmware OK only proves the report was")
        say("  queued, not that the phone or xCloud reacted.")
        say()
        say("  Have ONE of these in front of you:")
        say("    1. a Gamepad Tester app  - best: every control is visible")
        say("    2. xCloud                - realistic, but only the D-pad and")
        say("                               A/B produce visible changes")
        say()
        say("  Answer honestly. Enter means 'not sure', which is recorded as")
        say("  unproven - never as a pass.")

    if not args.yes and not args.dry_run:
        say()
        try:
            input("  Press Enter when the phone is ready (Ctrl+C to abort) ... ")
        except (EOFError, KeyboardInterrupt):
            say("\n  aborted")
            return 2

    # -- open the link -----------------------------------------------------
    say()
    try:
        pad = AndroidPad(cfg, args.transport, args.port, args.dry_run)
    except KeyError as exc:
        say(f"ERROR: {exc}")
        return 3

    if not pad.opened and not args.dry_run:
        say()
        say("  The link did not open, so no control could be tested.")
        say("  Run 2-CHECK.bat, or `python host/pad_link.py --check`.")
        return 3

    status = {"port": pad.link.port, "firmware": pad.link.firmware,
              "transport": pad.link.transport,
              "pad_connected": pad.link.pad_connected}

    # The handshake's most diagnostic bit, surfaced BEFORE the test rather than
    # after: if no host has enumerated the pad, every control will be accepted
    # and none will arrive, and knowing that now saves answering "no" 25 times.
    if status["pad_connected"] is False:
        say()
        say("  " + "!" * 62)
        say("  WARNING: the board is alive but NO USB HOST has enumerated the")
        say("  pad. Every command below will be accepted by the firmware and")
        say("  NONE will reach the phone, so expect every control to fail.")
        say()
        say("  Usual causes: the Leonardo's ON LED is dark (the phone is not")
        say("  powering it), the OTG adapter is at the BOARD end instead of the")
        say("  PHONE end, or the cable is charge-only.")
        say("  " + "!" * 62)
        if not args.yes and not args.dry_run:
            try:
                if input("  Test anyway? [y/N]: ").strip().lower() not in (
                        "y", "yes"):
                    pad.close()
                    return 2
            except (EOFError, KeyboardInterrupt):
                pad.close()
                return 2

    started = time.time()
    completed = True
    try:
        completed = run_controls(pad, controls, args)

        if completed and args.macros:
            say()
            say(BAR)
            say("  MACROS")
            say(BAR)
            for name in cfg.macros:
                say(f"\n  running macro '{name}' ...")
                pad.run_macro(name)
                if not args.auto:
                    try:
                        input("      Enter to continue ... ")
                    except (EOFError, KeyboardInterrupt):
                        break
    except KeyboardInterrupt:
        say("\n\n  Ctrl+C - releasing every control ...")
        completed = False
    finally:
        # ALWAYS release. pad_link.py's own docs make the point: a crash
        # mid-stick leaves an axis deflected and the character walking into a
        # wall long after this process is gone.
        pad.close()

    elapsed = time.time() - started
    if not completed:
        say()
        say("  Run ended early - the table below covers only what was tested.")

    return summarise(controls, elapsed, status, args)


if __name__ == "__main__":
    sys.exit(main())
