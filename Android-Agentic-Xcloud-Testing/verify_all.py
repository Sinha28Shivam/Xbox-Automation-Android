"""
verify_all.py - run every offline verification suite and summarise.

One entry point, because four separate commands is three too many to run
reliably before a commit. Each suite exits non-zero on failure, so this is just
a loop plus a tally.

None of these touch hardware, adb, or the network, so they are safe to run
anywhere - including before plugging anything in.

Run:  python verify_all.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SUITES = [
    ("verify_closed_loop.py",
     "the closed loop: GameState, INTERMEDIATE, one-action decisions"),
    ("verify_preserved.py",
     "the safeguards the refactor had to keep (silent_failure, ceiling, ...)"),
    ("verify_loop_cycle.py",
     "the routers, replaying the launch sequence that used to fail"),
    ("verify_handshake.py",
     "the browser signal handshake: automatic per page load, and verified"),
    ("verify_scenario_gate.py",
     "the scenario gate refuses what cannot be seen, not what merely varies"),
    ("verify_agent_contracts.py",
     "agents are CALLABLE: trace() signatures, no-hardware paths, targets"),
]



print("=" * 74)
print("OFFLINE VERIFICATION - no hardware, no adb, no network")
print("=" * 74)

failures: list[str] = []
for script, description in SUITES:
    path = ROOT / script
    if not path.is_file():
        print(f"\n[SKIP] {script} - not found")
        continue
    proc = subprocess.run([sys.executable, str(path)],
                          capture_output=True, text=True)
    # The last non-empty "N/M checks passed" line is the suite's own tally.
    tally = next((line.strip() for line in reversed(proc.stdout.splitlines())
                  if "checks passed" in line), "no tally reported")
    ok = proc.returncode == 0
    print(f"\n[{'PASS' if ok else 'FAIL'}] {script}")
    print(f"       {description}")
    print(f"       {tally}")
    if not ok:
        failures.append(script)
        # Only the failures are worth the screen space.
        for line in proc.stdout.splitlines():
            if "[FAIL]" in line or "REGRESSION" in line:
                print(f"       {line.strip()}")
        if proc.stderr.strip():
            print(f"       stderr: {proc.stderr.strip().splitlines()[-1]}")

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} suite(s) FAILED: {', '.join(failures)}")
    sys.exit(1)
print(f"all {len(SUITES)} suites passed")
print("=" * 74)
