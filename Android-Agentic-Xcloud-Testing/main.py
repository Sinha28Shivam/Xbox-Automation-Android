"""
main.py - THE ENTRY POINT. Start reading the project here.

    python main.py --check                    is the rig ready?
    python main.py --capabilities             what can this run do?
    python main.py "press A and watch the screen"
    python main.py --suite smoke              run a named suite
    python main.py --case controller_detected run one test case
    python main.py --list                     show every suite and case

WHAT IS WIRED TO WHAT
---------------------
This file exists to make the dependency graph legible in one screen. Everything
below it is a layer, and each layer only knows about the one beneath:

    main.py                 you are here - argument parsing lives in cli.py
      └─ agentic/cli.py     flags -> Settings overrides, exit codes
          └─ graph.py       the LangGraph state machine
              ├─ agents/    the seven specialists, in order:
              │               device    is the phone connected + signalable?
              │               scenario  is this testable? (may refuse)
              │               planner   compose steps from REAL capabilities
              │               executor  act -> settle -> observe -> judge
              │               evaluator verdict vs acceptance criteria
              │               rca       WHICH LAYER broke, + how to disprove
              │               reporter  JSON / Markdown / HTML
              ├─ tools/     the only ways to touch the world:
              │               pad.py      -> ../host/pad_link.py (VERIFIED)
              │               android.py  -> adb (screenshots, logcat, PWA)
              │               vision.py   -> frame diff, OCR, vision LLM
              └─ state.py   the shared GraphState + the live RunContext

    config/agentic.yaml     HOW agents behave      (models, timings, safety)
    ../config/controls.yaml WHICH controls exist   (read at runtime)
    scenarios/              WHAT to test           (free text, no schema)

Why `main.py` is thin: the real entry point is `agentic.cli:main`, so that
`python main.py` and `python -m agentic` cannot drift apart and behave
differently. This file adds exactly one thing - it puts its own directory on
sys.path, so the project runs from any working directory without installation.
That matters here because the .bat files run from the project root while a
developer runs python from inside this folder.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `agentic` importable no matter where python was invoked from. Inserting
# at position 0 is deliberate: this package must win over any similarly named
# module that happens to be on the path.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agentic.cli import main  # noqa: E402  (import must follow the path fix)

if __name__ == "__main__":
    sys.exit(main())
