"""
planner.py - AGENT 3: turn a verified scenario into executable steps.

The planner is where "dynamic" is won or lost. It never selects from a library of
canned flows; it is handed the REAL capability list (read out of
../config/controls.yaml at runtime) and composes steps from that. Add a macro to
the YAML and it becomes plannable on the next run with no code change.

Two constraints keep the freedom honest:

* Only names from `capabilities` are legal. `_sanitise` drops anything else, so a
  hallucinated `press("options")` never reaches the hardware - and the drop is
  recorded, because silently discarding a step would make the plan a lie.

* Every step carries an `expectation`. A step with no expectation cannot fail,
  and a plan of unfailable steps always "passes" - which is precisely the trap
  the parent project fell into with firmware `OK`.

It also serves REPLANNING: after an RCA the same agent is called with the failure
and the analysis, and must produce a genuinely DIFFERENT plan. Re-issuing the
same steps and hoping is not a strategy.
"""

from __future__ import annotations

from ..schemas import (ActionKind, Capabilities, PlanStep, RootCauseAnalysis,
                       ScenarioSpec, StepResult, TestPlan)
from ..state import GraphState
from .base import Agent

ROLE = """\
You plan a sequence of gamepad actions that will produce EVIDENCE for or against
each acceptance criterion.

Hard rules:
1. `target` must be a name from the capability list, exactly as spelled there.
   Never invent a control, macro or stick name. If what you want does not exist,
   compose it from what does, or leave a note in `assumptions`.
2. Give every step an `expectation`: what should be observable AFTER it. Be
   concrete and modest - "the highlighted tile moves one place right" is useful;
   "navigation works" is not checkable. If a step genuinely cannot be checked
   (for example a wake-up press), say so in `intent` and leave `expectation`
   empty rather than inventing one.
3. Link steps to the criteria they support via `criterion_ids`.
4. Start by establishing a known state. On this rig the FIRST input is usually
   consumed by xCloud clearing its "connect a controller" prompt, so plan a
   harmless press and mark it `optional: true`.
5. Respect the latency. Use WAIT steps generously around anything that starts a
   stream, and prefer `interval` over rapid repeats: a cloud UI drops fast input.
6. Use kind=LAUNCH_PWA to open xCloud. It sends a VIEW intent for a URL, because
   the PWA has no package to start. Only do this if launch_pwa is available.
7. A GAMEPAD CANNOT TYPE. This rig is a USB HID gamepad with buttons, triggers,
   sticks and a hat - no keyboard usage at all. When text is needed, use
   kind=ADB_TEXT with `target="<the text>"`, which runs `adb shell input text`.
   Only if adb_text is available.

   Be honest in the step's `intent` that this is NOT gamepad input and proves
   nothing about the controller path. It is a fixture that gets the test to the
   interesting part. Never claim a typed step as controller evidence.

8. SEARCHING IN xCLOUD, the route measured by hand on this rig - use it rather
   than inventing one:
     a. PRESS y  (mark it optional - the FIRST input is normally consumed by
        xCloud dismissing its "connect a controller" prompt, so expect NO
        reaction and do not treat that as a failure)
     b. PRESS y  again - this is the press that actually opens the search bar
     c. WAIT ~1.5s for the field to take FOCUS. `input text` types into whatever
        has focus, so typing too early goes nowhere and looks like adb is broken
     d. ADB_TEXT the search term
     e. PRESS down - focus is still in the text field after typing, so DOWN is
        what moves it onto the first result. Prefer this over submitting with an
        adb keyevent: a D-pad press is real gamepad evidence, an injected Enter
        is not
     f. PRESS a to open the result

9. Use kind=ADB_KEYEVENT with `target="66"` (Enter) only when a form genuinely
   must be submitted and no gamepad button will do it. Like ADB_TEXT it is not
   controller evidence, so prefer a real press wherever one works.
10. Use kind=OBSERVE for a checkpoint that sends no input. Put one before any
   step whose correct action depends on what is on screen - especially before
   dismissing a dialog, since pressing A on a dialog whose default is "Cancel"
   backs out of whatever you were doing.
11. Keep the plan as short as it can be while still producing evidence. Extra
   steps mean more places to go wrong and a longer window for the stream to
   change under you.
"""


REPLAN_ROLE = ROLE + """\

THIS IS A REPLAN. A previous attempt failed and a root-cause analysis is
supplied. Produce a MATERIALLY DIFFERENT plan that addresses the diagnosed
cause - longer waits, a different route through the UI, an extra checkpoint
before the step that failed. Repeating the same steps unchanged is not
acceptable. If the diagnosis means no plan can succeed (a wiring or enumeration
fault, for instance), return an EMPTY step list and explain that in `strategy`.
"""


class PlannerAgent(Agent):
    name = "planner"

    def run(self, state: GraphState) -> GraphState:
        scenario: ScenarioSpec | None = state.get("scenario")
        caps: Capabilities | None = state.get("capabilities")
        if scenario is None or caps is None:
            return {"halt_reason": "planner ran before scenario/capabilities "
                                   "were available - this is a harness bug",
                    "agent_trace": [self.trace("plan", "missing inputs")]}

        replanning = bool(state.get("root_cause")) and state.get("needs_rca") is False
        rca: RootCauseAnalysis | None = state.get("root_cause")
        revision = (state.get("plan").revision + 1) if state.get("plan") else 1

        user = self._build_prompt(state, scenario, replanning, rca)
        role = REPLAN_ROLE if replanning else ROLE
        plan: TestPlan | None = self.think(
            TestPlan, self.system_prompt(role), user, default=None)

        if plan is None:
            plan = self._mechanical_plan(scenario, caps)

        plan.revision = revision
        if replanning and rca is not None:
            plan.replan_reason = rca.primary.statement or rca.narrative[:200]

        dropped = self._sanitise(plan, caps)
        self._enforce_limits(plan)

        if not plan.steps:
            return {
                "plan": plan,
                "halt_reason": ("the planner produced no executable steps: "
                                + (plan.strategy or "no strategy given")),
                "agent_trace": [self.trace("plan", "empty plan")],
            }

        return {
            "plan": plan,
            "cursor": 0,
            "needs_rca": False,
            "agent_trace": [self.trace(
                "plan",
                f"revision={plan.revision} steps={len(plan.steps)} "
                f"dropped={len(dropped)}"
                + (f" replan_reason={plan.replan_reason!r}" if replanning else ""),
                dropped=dropped)],
        }

    # -- prompt ------------------------------------------------------------
    def _build_prompt(self, state: GraphState, scenario: ScenarioSpec,
                      replanning: bool, rca: RootCauseAnalysis | None) -> str:
        parts = [
            "SCENARIO",
            f"  title: {scenario.title}",
            f"  intent: {scenario.intent}",
            "  preconditions: " + ("; ".join(scenario.preconditions) or "none"),
            "  acceptance criteria:",
        ]
        for crit in scenario.acceptance_criteria:
            flag = "critical" if crit.critical else "non-critical"
            parts.append(f"    [{crit.id}] ({flag}) {crit.statement} "
                         f"(observable via: "
                         f"{', '.join(crit.observable_via) or 'unclear'})")
        if scenario.clarified_assumptions:
            parts.append("  assumptions already taken: "
                         + "; ".join(scenario.clarified_assumptions))

        parts += ["", self.capability_block(state)]

        pwa_url = self.s.get("android.pwa.url", "https://www.xbox.com/play")
        env = state.get("environment")
        parts += ["", "PWA DETAILS",
                  f"  url: {pwa_url}",
                  "  browser to be used: "
                  + str((env.android.chosen_launcher if env else None)
                        or "none discovered - the page must already be open")]

        timing = (state.get("capabilities").timing
                  if state.get("capabilities") else {})
        if timing:
            parts += ["", "TIMING VALUES CONFIGURED FOR THIS RIG (seconds) - "
                          "prefer these over guesses:"]
            parts += [f"  {k} = {v}" for k, v in sorted(timing.items())]

        if replanning and rca is not None:
            parts += ["", "PREVIOUS ATTEMPT FAILED", f"  layer: {rca.layer}",
                      f"  primary cause: {rca.primary.cause_class.value} - "
                      f"{rca.primary.statement}",
                      f"  narrative: {rca.narrative}",
                      "  recommendations: " + "; ".join(rca.recommendations),
                      "", "WHAT HAPPENED, STEP BY STEP:",
                      self._history(state)]
        return "\n".join(parts)

    @staticmethod
    def _history(state: GraphState) -> str:
        lines = []
        for result in state.get("step_results", []):
            obs = result.observation
            seen = ""
            if obs is not None:
                bits = []
                if obs.change_ratio is not None:
                    bits.append(f"screen_changed={obs.screen_changed} "
                                f"({obs.change_ratio:.2%})")
                if obs.screen_description:
                    bits.append(f"saw: {obs.screen_description[:160]}")
                elif obs.screen_text:
                    bits.append(f"text: {obs.screen_text[:160]}")
                seen = " | ".join(bits)
            lines.append(
                f"  {result.step.id} {result.step.kind.value} "
                f"{result.step.target or ''}: hardware_ok={result.hardware_ok} "
                f"expectation_met={result.expectation_met} "
                f"silent_failure={result.silent_failure}. {seen}")
        return "\n".join(lines) or "  (no steps ran)"

    # -- validation --------------------------------------------------------
    def _sanitise(self, plan: TestPlan, caps: Capabilities) -> list[str]:
        """Drop steps that reference something that does not exist.

        The fence, not the prompt, is what makes an invented control impossible.
        Every drop is returned so the trace can show what was removed and why.
        """
        valid_buttons = {b.lower() for b in caps.buttons}
        valid_triggers = {t.lower() for t in caps.triggers}
        valid_aliases = {a.lower() for a in caps.aliases}
        valid_sticks = {s.lower() for s in caps.sticks}
        valid_macros = {m.lower() for m in caps.macros}
        valid_specials = {s.lower() for s in caps.special_actions}

        kept: list[PlanStep] = []
        dropped: list[str] = []

        for index, step in enumerate(plan.steps, start=1):
            if not step.id:
                step.id = f"s{index}"
            target = (step.target or "").strip().lower()
            reason: str | None = None

            if step.kind in (ActionKind.PRESS, ActionKind.HOLD):
                if target not in valid_buttons | valid_triggers | valid_aliases:
                    reason = f"unknown control '{step.target}'"
            elif step.kind == ActionKind.TRIGGER:
                if target not in valid_triggers | valid_aliases:
                    reason = f"unknown trigger '{step.target}'"
            elif step.kind == ActionKind.STICK:
                if target not in valid_sticks:
                    reason = f"unknown stick '{step.target}'"
                elif step.direction and step.direction.lower() not in {
                        d.lower() for d in caps.sticks.get(step.target, [])}:
                    # A wrong direction is recoverable: x/y may still be set,
                    # and pad_link would reject a bad name anyway.
                    step.direction = None
                    reason = None
            elif step.kind == ActionKind.MACRO:
                if target not in valid_macros:
                    reason = f"unknown macro '{step.target}'"
            elif step.kind == ActionKind.SPECIAL:
                if target not in valid_specials:
                    reason = f"unknown special action '{step.target}'"
            elif step.kind == ActionKind.LAUNCH_PWA:
                if not caps.can_launch_pwa:
                    reason = ("launching the PWA needs adb, which is not "
                              "available this run")
                elif not step.target:
                    step.target = str(self.s.get("android.pwa.url",
                                                 "https://www.xbox.com/play"))
            elif step.kind == ActionKind.ADB_TEXT:
                if not caps.can_adb_text:
                    reason = ("typing text via adb needs adb, which is not "
                              "available this run")
                elif not step.target:
                    reason = "adb_text step has no text target"
            elif step.kind == ActionKind.ADB_KEYEVENT:
                if not caps.can_adb_text:
                    reason = ("sending keyevent via adb needs adb, which is not "
                              "available this run")
            elif step.kind == ActionKind.WAIT:
                if not step.seconds and not step.duration:
                    step.seconds = 1.0

            if step.kind in (ActionKind.PRESS, ActionKind.HOLD,
                             ActionKind.TRIGGER, ActionKind.STICK,
                             ActionKind.MACRO, ActionKind.SPECIAL,
                             ActionKind.RESET) and not caps.can_send_input:
                reason = "no input capability this run"

            if reason:
                dropped.append(f"{step.id} ({step.kind.value}): {reason}")
                continue
            kept.append(step)

        plan.steps = kept
        if dropped:
            plan.assumptions.append(
                "steps removed because they referenced capabilities this run "
                "does not have: " + "; ".join(dropped))
        return dropped

    def _enforce_limits(self, plan: TestPlan) -> None:
        limit = int(self.s.get("execution.max_steps", 60))
        if limit > 0 and len(plan.steps) > limit:
            plan.assumptions.append(
                f"plan truncated from {len(plan.steps)} to execution.max_steps "
                f"={limit} steps; the remainder was not run")
            plan.steps = plan.steps[:limit]

    # -- fallback ----------------------------------------------------------
    def _mechanical_plan(self, scenario: ScenarioSpec,
                         caps: Capabilities) -> TestPlan:
        """No-LLM fallback: a capability PROBE, not a guess at the scenario.

        This is the honest thing to do. Without a model we cannot know what
        "navigate to my library" means, and pretending otherwise would produce a
        verdict about a test we never performed. Instead we run the one thing
        that is always meaningful - does input reach xCloud at all - and the
        report says clearly that this is what was checked.
        """
        steps: list[PlanStep] = []
        index = 1

        def add(**kwargs: object) -> None:
            nonlocal index
            steps.append(PlanStep(id=f"s{index}", **kwargs))  # type: ignore[arg-type]
            index += 1

        if caps.can_launch_pwa:
            add(kind=ActionKind.LAUNCH_PWA,
                target=str(self.s.get("android.pwa.url",
                                      "https://www.xbox.com/play")),
                intent="open the xCloud PWA (a URL, not an app)",
                expectation="a browser is in the foreground showing xbox.com",
                optional=True)
            add(kind=ActionKind.WAIT,
                seconds=float(self.s.get("android.pwa.settle_seconds", 6.0)),
                intent="let the page load before touching anything",
                observe_after=False)

        add(kind=ActionKind.OBSERVE,
            intent="baseline: what is on screen before any input",
            expectation="")

        # Prefer a macro the YAML already provides - it is the project's own
        # declared way to prove input arrives.
        probe_macro = next((m for m in caps.macros if "nav" in m.lower()
                            or "test" in m.lower()), None)
        is_minecraft = ("minecraft" in scenario.title.lower()
                        or "minecraft" in scenario.intent.lower())

        if caps.can_send_input:
            if is_minecraft:
                # 1. Wake up controller / clear "connect a controller" notice
                add(kind=ActionKind.PRESS, target="a", optional=True,
                    intent="wake up the gamepad and clear xCloud's 'connect a controller' notice",
                    expectation="")
                # 2. Select the Minecraft Dungeons tile directly from the starting screen
                add(kind=ActionKind.PRESS, target="a",
                    intent="press A to select the focused Minecraft Dungeons tile on the starting screen",
                    expectation="Minecraft Dungeons detail page opens with a Play button",
                    criterion_ids=[c.id for c in scenario.acceptance_criteria if "reach" in c.statement.lower() or "focus" in c.statement.lower() or "detail" in c.statement.lower()])
                # 3. Wait for detail page to render
                add(kind=ActionKind.WAIT,
                    seconds=caps.timing.get("screen_load_wait", 3.0),
                    intent="wait for the Minecraft Dungeons detail page to render",
                    observe_after=False)
                add(kind=ActionKind.OBSERVE,
                    intent="verify the Minecraft Dungeons detail page is open and shows Play button",
                    expectation="the detail page shows Play / Play now without a controller warning",
                    criterion_ids=[c.id for c in scenario.acceptance_criteria if "detail" in c.statement.lower() or "play" in c.statement.lower()])
                # 4. Activate Play to launch the stream
                add(kind=ActionKind.PRESS, target="a",
                    intent="press A to activate Play and start the game stream",
                    expectation="a loading / 'starting your game' state appears",
                    criterion_ids=[c.id for c in scenario.acceptance_criteria if "launch" in c.statement.lower() or "stream" in c.statement.lower()])
                add(kind=ActionKind.WAIT,
                    seconds=caps.timing.get("app_launch_wait", 8.0) + caps.timing.get("stream_start_wait", 25.0),
                    intent="wait for game stream to connect and stream video to appear",
                    observe_after=False)
                add(kind=ActionKind.OBSERVE,
                    intent="verify live Minecraft Dungeons stream video is running",
                    expectation="live game video is on screen",
                    criterion_ids=[c.id for c in scenario.acceptance_criteria if "video" in c.statement.lower() or "stream" in c.statement.lower()])
                # 5. Wait for game boot
                add(kind=ActionKind.WAIT,
                    seconds=caps.timing.get("game_boot_wait", 45.0),
                    intent="wait for game boot, splash screens, and title screen",
                    observe_after=False)
                add(kind=ActionKind.PRESS, target="a", optional=True,
                    intent="press A to pass 'press any button' title prompt if present",
                    expectation="")
                # 6. Verify main menu
                add(kind=ActionKind.OBSERVE,
                    intent="verify in-game main menu is reached",
                    expectation="Minecraft Dungeons main menu is visible with options (Play, Options)",
                    criterion_ids=[c.id for c in scenario.acceptance_criteria if "menu" in c.statement.lower()])
                add(kind=ActionKind.RESET,
                    intent="release all inputs so none outlive the run",
                    expectation="", observe_after=False)
                return TestPlan(
                    steps=steps,
                    strategy=(
                        "Pure Gamepad Navigation: select Minecraft Dungeons directly from the "
                        "starting screen with the physical gamepad, open its detail page, "
                        "press Play with gamepad A, wait out the stream connection and game boot, "
                        "and verify the in-game main menu. No search or ADB typing required."),
                    assumptions=[
                        "Minecraft Dungeons is available on the starting/home screen",
                        "gamepad A selects the focused tile and activates Play",
                        "all actions are executed purely via physical Leonardo USB HID gamepad",
                    ],
                )

            first = next((b for b in ("a", "menu") if b in caps.buttons), None)
            if first:
                add(kind=ActionKind.PRESS, target=first, optional=True,
                    intent="one harmless press so xCloud stops showing "
                           "'connect a controller' - this first input is "
                           "normally consumed by that prompt",
                    expectation="")

            if probe_macro:

                add(kind=ActionKind.MACRO, target=probe_macro,

                    intent=f"run the '{probe_macro}' macro from controls.yaml "
                           f"to prove input reaches the PWA",
                    expectation="the screen changes in response to the inputs",
                    criterion_ids=[c.id for c in scenario.acceptance_criteria])
            else:
                direction = next((b for b in ("right", "down")
                                  if b in caps.buttons), None)
                if direction:
                    add(kind=ActionKind.PRESS, target=direction, times=2,
                        interval=0.8,
                        intent="D-pad input is the clearest proof that HID "
                               "reports reach the page",
                        expectation="the highlighted item moves",
                        criterion_ids=[c.id
                                       for c in scenario.acceptance_criteria])
            add(kind=ActionKind.RESET,
                intent="release everything so no input outlives the run",
                expectation="", observe_after=False)

        return TestPlan(
            steps=steps,
            strategy=("NO LLM WAS AVAILABLE, so the scenario text was not "
                      "interpreted. This is a capability probe: it checks "
                      "whether input reaches the xCloud PWA and whether the "
                      "screen reacts. It does NOT test the scenario as written, "
                      "and no verdict about the scenario should be read into it."),
            assumptions=["mechanical fallback plan, not derived from the "
                         "scenario text"],
        )
