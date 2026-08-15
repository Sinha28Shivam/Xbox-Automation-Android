"""
pad.py - the ACTUATOR. Wraps the verified ../host/pad_link.py.

Deliberately a thin wrapper, not a reimplementation. pad_link.py is the code
that was proven on hardware (8/8 HID reports, xCloud recognises the pad); the
agentic layer earns nothing by duplicating its protocol and risks disagreeing
with it. So this module:

  * imports AndroidPad/ControlConfig by path (they are not an installed package)
  * exposes ONE `dispatch(step)` entry point the executor calls
  * derives Capabilities from controls.yaml, so the LLM is told the real button
    list rather than being trusted to remember it
  * enforces `safety.forbidden_controls` in CODE

WHY dispatch() RETURNS (ok, detail) AND NOTHING ELSE
---------------------------------------------------
`ok` means the firmware replied OK - the HID report was queued. It does NOT
mean xCloud reacted. That distinction is the entire lesson of the parent
project, so this layer refuses to imply more than it knows; proving the app
reacted is the ObserverAgent's job, from pixels.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import time
from pathlib import Path
from typing import Any

from ..schemas import ActionKind, Capabilities, PadStatus, PlanStep
from ..settings import Settings


def _load_pad_module(host_dir: Path) -> Any:
    """Import pad_link.py from an arbitrary directory.

    host/ is a sibling folder, not a package, and there is no __init__.py. A
    spec-based import keeps that intact: no sys.path pollution that could later
    shadow an unrelated module named `serial` or `config`.
    """
    module_path = host_dir / "pad_link.py"
    if not module_path.is_file():
        raise FileNotFoundError(
            f"pad_link.py not found at {module_path}. Set hardware.pad_module_dir "
            f"in config/agentic.yaml to the folder that contains it.")
    spec = importlib.util.spec_from_file_location("pad_link", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pad_link"] = module
    spec.loader.exec_module(module)
    return module


class PadTool:
    """Owns the serial link for the whole run. Exactly one instance."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.status = PadStatus()
        self.pad: Any = None
        self.cfg: Any = None
        self._module: Any = None
        self.forbidden = {str(c).lower()
                          for c in settings.get_list("safety.forbidden_controls")}
        # pad_link.py prints progress to stdout. We capture it per-command so
        # those lines become report evidence instead of console noise.
        self.transcript: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> PadStatus:
        """Open the link and handshake. Never raises: a dead link is a normal,
        reportable outcome (BLOCKED), not a stack trace."""
        dry_run = bool(self.s.get("hardware.dry_run", False))
        self.status.dry_run = dry_run

        try:
            host_dir = self.s.resolve_path("hardware.pad_module_dir", "../host")
            self._module = _load_pad_module(host_dir)
            controls = self.s.resolve_path("hardware.controls_config",
                                           "../config/controls.yaml")
            self.cfg = self._module.ControlConfig(controls)
        except (FileNotFoundError, ImportError, KeyError) as exc:
            self.status.error = f"cannot load the hardware layer: {exc}"
            self.status.diagnostics.append(
                "This is a harness/setup fault, not a device fault - no test "
                "conclusion can be drawn from it.")
            return self.status

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                self.pad = self._module.AndroidPad(
                    config=self.cfg,
                    transport=self.s.get("hardware.transport"),
                    port=self.s.get("hardware.serial_port"),
                    dry_run=dry_run,
                )
        except Exception as exc:                     # noqa: BLE001
            self.status.error = f"{type(exc).__name__}: {exc}"
            self.transcript.append(buf.getvalue())
            return self.status

        out = buf.getvalue()
        self.transcript.append(out)

        link = self.pad.link
        self.status.link_open = bool(self.pad.opened or dry_run)
        self.status.port = link.port
        self.status.firmware = link.firmware
        self.status.transport = link.transport
        self.status.pad_connected_to_phone = link.pad_connected

        # Turn pad_link's own printed hints into structured diagnostics so the
        # RCA agent reasons over them instead of over an opaque bool.
        for line in out.splitlines():
            stripped = line.strip()
            if stripped and (stripped.startswith(">>") or "ERROR" in stripped
                             or "FAILED" in stripped):
                self.status.diagnostics.append(stripped)

        if not self.status.link_open:
            self.status.error = self.status.error or (
                "the board did not answer PING on "
                f"{self.status.port or 'any port'}")
        elif self.status.pad_connected_to_phone is False:
            # The board is alive but no USB host has enumerated the pad. On this
            # rig that means the phone is not in OTG host mode - the parent
            # README's "ON LED dark" symptom. Worth flagging loudly: every
            # input will be accepted and none will arrive.
            self.status.diagnostics.append(
                "board is alive but NO host has enumerated the pad: the phone "
                "is probably not in OTG host mode (check the OTG adapter is at "
                "the PHONE end and the Leonardo's ON LED is lit)")
        return self.status

    def close(self) -> None:
        """Always release inputs. A held stick outlives this process otherwise."""
        if self.pad is not None and self.s.get("execution.always_reset_on_exit",
                                               True):
            with contextlib.suppress(Exception):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.pad.close()
        self.pad = None

    # -- capability discovery ---------------------------------------------
    def capabilities(self) -> Capabilities:
        """Read the REAL control surface out of controls.yaml.

        Nothing is hardcoded here: add a macro to the YAML and the planner can
        use it on the next run with no code change. That is the whole point.
        """
        caps = Capabilities()
        if self.cfg is None:
            caps.unavailable.append(
                "controls.yaml could not be read, so no input is possible")
            return caps

        caps.buttons = [b for b in self.cfg.buttons if b.lower() not in self.forbidden]
        caps.triggers = [t for t in self.cfg.triggers if t.lower() not in self.forbidden]
        caps.sticks = {name: list((spec.get("directions") or {}).keys())
                       for name, spec in self.cfg.sticks.items()}
        caps.macros = {name: str(spec.get("description", ""))
                       for name, spec in self.cfg.macros.items()}
        caps.special_actions = {name: str(spec.get("description", ""))
                               for name, spec in self.cfg.special.items()}
        caps.aliases = {alias: canonical
                        for alias, (_kind, canonical)
                        in getattr(self.cfg, "_alias_map", {}).items()}
        caps.timing = {k: float(v) for k, v in (self.cfg.timing or {}).items()
                       if isinstance(v, (int, float))}
        caps.can_send_input = bool(self.status.link_open)

        if self.forbidden:
            caps.unavailable.append(
                f"controls blocked by safety.forbidden_controls: "
                f"{', '.join(sorted(self.forbidden))}")
        if not self.status.link_open:
            caps.unavailable.append(
                f"input: {self.status.error or 'the pad link is not open'}")
        return caps

    def state(self) -> str | None:
        """Ask the board what it currently holds - an independent second source
        to compare against what we THINK we sent."""
        if self.pad is None:
            return None
        with contextlib.suppress(Exception):
            with contextlib.redirect_stdout(io.StringIO()):
                return self.pad.link.state()
        return None

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, step: PlanStep) -> tuple[bool, str]:
        """Execute one PlanStep. Returns (firmware_ok, captured_detail).

        The dispatch table mirrors pad_link.py's public API exactly, so there is
        no translation logic to be subtly wrong.
        """
        if self.pad is None:
            return False, "pad link is not open"

        target = (step.target or "").strip()
        if target and target.lower() in self.forbidden:
            # Enforced here, not in a prompt. A prompt is a request; this is a
            # wall - the model cannot talk its way past it.
            return False, (f"control '{target}' is listed in "
                           f"safety.forbidden_controls and was not sent")

        buf = io.StringIO()
        ok = False
        try:
            with contextlib.redirect_stdout(buf):
                ok = self._do(step, target)
        except KeyError as exc:
            # pad_link raises KeyError for an unknown control name. That is a
            # planner error worth naming precisely, because the fix is to widen
            # the prompt's capability list, not to touch the hardware.
            return False, (f"unknown control {exc}. Valid names come from "
                           f"controls.yaml - the planner used one that is not "
                           f"in it.")
        except Exception as exc:                     # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}\n{buf.getvalue()}"

        detail = buf.getvalue().strip()
        self.transcript.append(detail)
        return bool(ok), detail

    def _do(self, step: PlanStep, target: str) -> bool:
        pad = self.pad
        kind = step.kind

        if kind == ActionKind.PRESS:
            return pad.press_times(target, step.times, step.duration,
                                   step.interval)
        if kind == ActionKind.HOLD:
            duration = step.duration or step.seconds or self.cfg.timing_value(
                "long_press_duration", 1.0)
            return pad.hold(target, float(duration))
        if kind == ActionKind.TRIGGER:
            return pad.trigger(target, step.value, step.duration)
        if kind == ActionKind.STICK:
            return pad.stick(target, step.direction, step.x, step.y,
                             step.duration)
        if kind == ActionKind.MACRO:
            return pad.run_macro(target)
        if kind == ActionKind.SPECIAL:
            return pad.run_special(target)
        if kind == ActionKind.RESET:
            return pad.link.reset()
        if kind == ActionKind.WAIT:
            time.sleep(float(step.seconds or step.duration or 1.0))
            return True
        # OBSERVE / LAUNCH_PWA / ASSERT are not pad actions; the executor routes
        # them elsewhere. Reaching here means the router has a bug, so say so
        # rather than returning a misleading True.
        return False
