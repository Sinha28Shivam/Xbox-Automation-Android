"""
base.py - what every agent shares.

An agent here is: a name, a RunContext, one `run(state) -> partial state` method,
and a documented DETERMINISTIC FALLBACK.

THE FALLBACK RULE
-----------------
Every agent must produce a usable result with no LLM at all. Not for elegance -
because a test harness whose verdict depends on an API key being present is not
a test harness. So:

    * LLM available  -> reasoned result, `llm_used=True`
    * LLM missing    -> mechanical result, `llm_used=False`, and the report says
                        which conclusions were mechanical

Mechanical conclusions are weaker and are labelled as such. What they never are
is silently absent.

The shared PWA context below is injected into every prompt: an agent that thinks
xCloud is an installed app will plan `am start` steps that cannot work, so the
correction has to be systemic rather than remembered per-prompt.
"""

from __future__ import annotations

from typing import Any

from ..llm import LLMUnavailable
from ..settings import Settings
from ..state import GraphState, RunContext, trace

# --------------------------------------------------------------------------
# Domain context shared by every prompt.
# --------------------------------------------------------------------------
SYSTEM_CONTEXT = """\
You are part of a multi-agent system that tests XBOX CLOUD GAMING (xCloud) on a
physical Android phone.

THE RIG - read carefully, it constrains everything you may propose:
* Gamepad input does NOT come from adb. A PC drives an Arduino Leonardo over a
  UART bridge; the Leonardo is wired to the phone by USB-OTG and presents itself
  as a REAL USB HID gamepad. So the only way to press a button is the pad tool.
* `adb shell input keyevent` is NOT a substitute. It injects a system key event,
  never an HID report, so it proves nothing about the controller path under test.
* xCloud IS NOT AN INSTALLED APP. It is a PWA - a web page in a mobile browser
  (sometimes installed as a WebAPK). Therefore:
    - there is no package to launch and no activity to assert on; the app is
      opened by sending a VIEW intent for its URL
    - the focused window during a test is a BROWSER package; that is correct and
      is not evidence of failure
    - browser chrome, an address bar or a tab strip may be visible
    - the version under test is whatever the server served today: it can change
      between two runs with no local change at all
* The stream adds 60-100 ms of network latency ON TOP of UI animation. Menu
  automation is practical; frame-exact input is not. When in doubt, wait longer.

THE HONESTY RULE, which outranks being helpful:
A firmware "OK" means an HID report was QUEUED. It does not mean the phone
reacted, and it certainly does not mean xCloud reacted. Only an observation of
the screen can support that claim. If the evidence does not settle a question,
say it is inconclusive. A confident wrong verdict is the worst thing this system
can produce.
"""


class Agent:
    """Base class. Subclasses implement `run`."""

    name: str = "agent"

    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.s: Settings = ctx.settings
        self.llm = ctx.llm
        self.llm_used = False
        self.notes: list[str] = []

    # -- LLM helpers -------------------------------------------------------
    def think(self, schema: type, system: str, user: str,
              default: Any = None) -> Any:
        """Structured LLM call that degrades instead of raising.

        Returns `default` when no model is usable, having recorded why. The
        caller then applies its own mechanical fallback.
        """
        try:
            result = self.llm.structured(self.name, schema, system, user)
            self.llm_used = True
            return result
        except LLMUnavailable as exc:
            self._record_llm_failure(str(exc))
            return default
        except Exception as exc:                     # noqa: BLE001
            self._record_llm_failure(f"{type(exc).__name__}: {exc}")
            return default

    def _record_llm_failure(self, detail: str) -> None:
        """Make a degraded run VISIBLE, not just quiet.

        Learned the hard way: a wrong model id in the config produced a full run
        of mechanical fallbacks whose report said only "no LLM was available".
        That is technically true and practically useless - the actual cause was a
        404 naming the bad model, and it never reached the reader.

        So the reason is pushed onto the factory's shared error list, which the
        report and `--capabilities` both print.
        """
        note = f"{self.name}: fell back to deterministic logic - {detail}"
        self.notes.append(note)
        if detail not in self.llm.errors:
            self.llm.errors.append(detail)

    def system_prompt(self, role: str) -> str:
        """SYSTEM_CONTEXT + this agent's specific job."""
        return f"{SYSTEM_CONTEXT}\nYOUR ROLE\n---------\n{role.strip()}\n"

    def capability_block(self, state: GraphState) -> str:
        """The real, discovered capabilities - the anti-hallucination fence."""
        caps = state.get("capabilities")
        if caps is None:
            return "CAPABILITIES: not yet discovered."
        return ("CAPABILITIES AVAILABLE THIS RUN (use ONLY these names):\n"
                + caps.summary_for_prompt())

    # -- tracing -----------------------------------------------------------
    def trace(self, action: str, detail: str = "", **extra: Any) -> dict[str, Any]:
        return trace(self.name, action, detail, llm=self.llm_used, **extra)

    # -- contract ----------------------------------------------------------
    def run(self, state: GraphState) -> GraphState:
        raise NotImplementedError
