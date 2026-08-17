"""
logbook.py - the TERMINAL LOG. One place that decides how a run narrates itself.

WHY THIS EXISTS
---------------
Before this module the only live output was `cli.py:_say`, which prints the
FINAL report. A 338-second run therefore looked like a frozen console: no way to
tell "waiting for the stream to settle" apart from "hung on a dead serial port".
Worse, when a run failed the only evidence of WHEN each thing happened was the
millisecond prefix on a screenshot filename.

So every layer that touches the world (pad, adb, vision, waits, the graph
itself) now narrates itself through this module. The rules:

* ONE writer. Two modules printing on their own schedule interleave badly and
  the transcript becomes unreadable exactly when you need it.
* MONOTONIC, RELATIVE timestamps. `t+12.4s` answers "how long did that take"
  directly; a wall clock makes you do arithmetic while debugging.
* ASCII ONLY by default. The Windows console is cp1252, and pad_link.py already
  documents what a stray byte does there. `_emit` degrades instead of raising:
  losing a run to a UnicodeEncodeError in a LOG LINE would be absurd.
* Colour is opt-out and never load-bearing. Every line is fully readable with
  the escape codes stripped, because CI logs strip them.
* A mirrored FILE log per run. The console scrolls; the file does not, and it
  lands next to the screenshots it describes.

The channel names (`ACT`, `WAIT`, `SEE`, `JUDGE`) are deliberately aligned to
one width so a reader can scan the left gutter and see the rhythm of the run -
act, settle, observe, judge - which is exactly the executor's contract.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, TextIO

# Level ordering. Anything below the configured threshold is dropped before it
# is even formatted, so a quiet run pays almost nothing for the instrumentation.
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "silent": 100}

# channel -> (label, colour). Labels are padded to 5 so the gutter lines up.
_CHANNELS: dict[str, tuple[str, str]] = {
    "run":    ("RUN  ", "\033[1;36m"),
    "step":   ("STEP ", "\033[1;37m"),
    "act":    ("ACT  ", "\033[0;35m"),
    "wait":   ("WAIT ", "\033[0;33m"),
    "see":    ("SEE  ", "\033[0;36m"),
    "judge":  ("JUDGE", "\033[0;34m"),
    "hw":     ("HW   ", "\033[0;35m"),
    "adb":    ("ADB  ", "\033[0;36m"),
    "llm":    ("LLM  ", "\033[0;34m"),
    "node":   ("NODE ", "\033[1;30m"),
    "ok":     ("OK   ", "\033[0;32m"),
    "warn":   ("WARN ", "\033[1;33m"),
    "error":  ("ERROR", "\033[1;31m"),
    "debug":  ("dbg  ", "\033[1;30m"),
}
_RESET = "\033[0m"


def _supports_colour(stream: TextIO) -> bool:
    """Colour only when a human is plausibly looking at it."""
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.name == "nt":
        # Windows 10+ consoles understand ANSI once virtual terminal processing
        # is enabled, which Python does for us when the handle is a real tty.
        return "WT_SESSION" in os.environ or "TERM" in os.environ \
            or sys.version_info >= (3, 6)
    return True


class Logbook:
    """Run-scoped terminal + file logger. Import the `log` singleton below."""

    def __init__(self) -> None:
        self.run_id: str = ""
        self.level: int = LEVELS["info"]
        self.started: float = time.monotonic()
        self.colour: bool = _supports_colour(sys.stdout)
        self._fh: TextIO | None = None
        self._path: Path | None = None
        # Cheap running totals, printed by `summary()`. Knowing that a 338s run
        # spent 210s asleep is the difference between "slow rig" and "too many
        # waits", and it is the question this task started from.
        self.counts: dict[str, int] = {}
        self.waited_seconds: float = 0.0

    # -- lifecycle ---------------------------------------------------------
    def configure(self, run_id: str = "", level: str | None = None,
                  file_path: Path | str | None = None,
                  colour: bool | None = None) -> None:
        """Start a run's log. Safe to call again; it closes the previous file."""
        self.close()
        self.run_id = run_id
        self.started = time.monotonic()
        self.counts = {}
        self.waited_seconds = 0.0
        if level is not None:
            self.level = LEVELS.get(str(level).lower(), LEVELS["info"])
        if colour is not None:
            self.colour = bool(colour) and _supports_colour(sys.stdout)
        if file_path:
            try:
                path = Path(file_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                # Never colour the file: escape codes make `type run.log`
                # unreadable and grep matches harder.
                self._fh = path.open("a", encoding="utf-8")
                self._path = path
            except OSError:
                # A log we cannot write is not worth failing a hardware run for.
                self._fh = None
                self._path = None

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
        self._fh = None

    @property
    def path(self) -> Path | None:
        return self._path

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    # -- emission ----------------------------------------------------------
    def _emit(self, channel: str, message: str, level: int,
              indent: int = 0) -> None:
        if level < self.level:
            return
        self.counts[channel] = self.counts.get(channel, 0) + 1

        label, colour = _CHANNELS.get(channel, ("     ", ""))
        stamp = f"t+{self.elapsed():7.1f}s"
        pad = "  " * indent
        plain = f"[{stamp}] {label} | {pad}{message}"

        if self._fh is not None:
            try:
                self._fh.write(plain + "\n")
                self._fh.flush()          # a crash must not eat the last line
            except OSError:
                self._fh = None

        text = (f"[{stamp}] {colour}{label}{_RESET} | {pad}{message}"
                if self.colour and colour else plain)
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            print(text.encode("ascii", "backslashreplace").decode("ascii"),
                  flush=True)
        except (OSError, ValueError):
            # A closed stdout (piped output that went away) must not kill a run
            # that is holding a serial port open.
            pass

    # -- channels ----------------------------------------------------------
    def run_start(self, message: str) -> None:
        self._emit("run", message, LEVELS["info"])

    def step(self, message: str) -> None:
        self._emit("step", message, LEVELS["info"])

    def act(self, message: str, indent: int = 1) -> None:
        self._emit("act", message, LEVELS["info"], indent)

    def wait(self, seconds: float, reason: str, indent: int = 1) -> None:
        self.waited_seconds += max(0.0, seconds)
        self._emit("wait", f"{seconds:.2f}s - {reason}", LEVELS["info"], indent)

    def see(self, message: str, indent: int = 1) -> None:
        self._emit("see", message, LEVELS["info"], indent)

    def judge(self, message: str, indent: int = 1) -> None:
        self._emit("judge", message, LEVELS["info"], indent)

    def hw(self, message: str, indent: int = 1) -> None:
        self._emit("hw", message, LEVELS["info"], indent)

    def adb(self, message: str, indent: int = 1) -> None:
        self._emit("adb", message, LEVELS["debug"], indent)

    def llm(self, message: str, indent: int = 1) -> None:
        self._emit("llm", message, LEVELS["debug"], indent)

    def node(self, message: str) -> None:
        self._emit("node", message, LEVELS["info"])

    def ok(self, message: str, indent: int = 0) -> None:
        self._emit("ok", message, LEVELS["info"], indent)

    def warn(self, message: str, indent: int = 0) -> None:
        self._emit("warn", message, LEVELS["warn"], indent)

    def error(self, message: str, indent: int = 0) -> None:
        self._emit("error", message, LEVELS["error"], indent)

    def debug(self, message: str, indent: int = 1) -> None:
        self._emit("debug", message, LEVELS["debug"], indent)

    # -- helpers -----------------------------------------------------------
    def rule(self, title: str = "") -> None:
        if LEVELS["info"] < self.level:
            return
        line = "-" * 68
        self._emit("run", f"{title}\n{line}" if title else line, LEVELS["info"])

    def kv(self, channel: str, **pairs: Any) -> None:
        """Log a set of fields on one line: `key=value key=value`."""
        body = " ".join(f"{k}={v}" for k, v in pairs.items())
        self._emit(channel, body, LEVELS["info"], indent=1)

    def summary(self) -> None:
        """Close the narrative with where the time actually went."""
        total = self.elapsed()
        share = (self.waited_seconds / total * 100.0) if total > 0 else 0.0
        self._emit(
            "run",
            f"log totals: {total:.1f}s elapsed, {self.waited_seconds:.1f}s "
            f"deliberately waiting ({share:.0f}%), "
            + " ".join(f"{k}={v}" for k, v in sorted(self.counts.items())),
            LEVELS["info"])
        if self._path is not None:
            self._emit("run", f"terminal log written to {self._path}",
                       LEVELS["info"])


# The singleton every module imports. A logger is exactly the kind of thing that
# must NOT be threaded through twelve constructors.
log = Logbook()
