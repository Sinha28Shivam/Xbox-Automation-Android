"""
reporter.py - AGENT 7: assemble the report.

Writes JSON, Markdown and HTML from the state. Three formats because they serve
different readers: JSON for CI and for re-reading a run months later, Markdown
for a pull request, HTML for a human looking at the screenshots.

The JSON is the primary artefact. It is the full state - every observation, every
judgement, the whole agent trace - so a verdict can always be re-examined without
the hardware, which is the difference between a test report and a claim.

The prose summary leads with WHAT WAS NOT PROVEN. That ordering is deliberate: the
caveats are the part a reader skips and the part that decides whether the verdict
means anything.
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path

from ..schemas import TestReport, Verdict
from ..state import GraphState
from .base import Agent

ROLE = """\
You write the executive summary of a test run, for an engineer who was not there.

Structure, in this order:
1. The verdict, in one sentence, with the confidence stated plainly.
2. WHAT WAS ACTUALLY PROVEN, and just as importantly WHAT WAS NOT. Be explicit
   about anything that was assumed, unobserved or unverifiable.
3. If it failed: the root cause and the single next action to take.
4. If it passed: what would make the result stronger next time.

Rules:
* At most 220 words. A report nobody reads protects nobody.
* Never overstate. "Input was accepted by the firmware" and "xCloud reacted" are
  different claims and must not be blurred.
* Plain prose. No headings, no bullet lists, no markdown.
"""


class ReporterAgent(Agent):
    name = "reporter"

    def run(self, state: GraphState) -> GraphState:
        started = self.ctx.started_at
        evaluation = state.get("evaluation")
        verdict = state.get("verdict") or (
            evaluation.verdict if evaluation else Verdict.INCONCLUSIVE)

        report = TestReport(
            run_id=state.get("run_id", "unknown"),
            started_at=time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(started)),
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            duration_seconds=round(self.ctx.elapsed(), 2),
            scenario=state.get("scenario"),
            environment=state.get("environment"),
            plan=state.get("plan"),
            step_results=list(state.get("step_results", [])),
            evaluation=evaluation,
            root_cause=state.get("root_cause"),
            verdict=verdict,
            replans=int(state.get("replans", 0)),
            artifacts=list(self.ctx.artifacts),
            agent_trace=list(state.get("agent_trace", [])),
        )

        report.recommendations = self._recommendations(state, report)
        report.executive_summary = self._summary(state, report)

        written = self._write(report)

        return {
            "report": report,
            "verdict": verdict,
            "agent_trace": [self.trace(
                "report",
                f"verdict={verdict.value} files={len(written)} "
                f"steps={len(report.step_results)}",
                files=[str(p) for p in written])],
        }

    # -- prose -------------------------------------------------------------
    def _summary(self, state: GraphState, report: TestReport) -> str:
        facts = self._facts(state, report)
        try:
            text = self.llm.text(self.name, self.system_prompt(ROLE), facts)
            self.llm_used = True
            if text:
                return text
        except Exception as exc:                     # noqa: BLE001
            self.notes.append(f"reporter: prose summary unavailable ({exc})")
        return self._mechanical_summary(report)

    def _facts(self, state: GraphState, report: TestReport) -> str:
        lines = [f"VERDICT: {report.verdict.value}",
                 f"duration: {report.duration_seconds}s   "
                 f"replans: {report.replans}"]
        if report.scenario:
            lines += [f"scenario: {report.scenario.title}",
                      f"intent: {report.scenario.intent}"]
            if report.scenario.ambiguities:
                lines.append("scenario ambiguities: "
                             + "; ".join(report.scenario.ambiguities))
        if report.environment:
            pad = report.environment.pad
            lines += [
                f"pad link: open={pad.link_open} firmware={pad.firmware} "
                f"host_enumerated_pad={pad.pad_connected_to_phone} "
                f"dry_run={pad.dry_run}",
                f"observation available: "
                f"{report.environment.capabilities.can_screenshot}",
            ]
            if report.environment.warnings:
                lines.append("environment warnings: "
                             + " | ".join(report.environment.warnings))
        if report.evaluation:
            lines.append(f"evaluation confidence: "
                         f"{report.evaluation.confidence:.2f}")
            lines.append("criteria:")
            for crit in report.evaluation.criteria:
                state_word = {True: "MET", False: "NOT MET",
                              None: "UNVERIFIED"}[crit.met]
                lines.append(f"  [{crit.criterion_id}] {state_word}: "
                             f"{crit.statement} - {crit.reasoning[:200]}")
            if report.evaluation.caveats:
                lines.append("caveats: " + " | ".join(report.evaluation.caveats))
        lines.append("steps: " + ", ".join(
            f"{r.step.id}({r.step.kind.value}"
            f"{'/silent_failure' if r.silent_failure else ''}"
            f"={r.expectation_met})" for r in report.step_results))
        if report.root_cause:
            rca = report.root_cause
            lines += [f"root cause layer: {rca.layer}",
                      f"primary cause: {rca.primary.cause_class.value} - "
                      f"{rca.primary.statement}",
                      f"discriminating test: "
                      f"{rca.primary.discriminating_test}"]
        return "\n".join(lines)

    @staticmethod
    def _mechanical_summary(report: TestReport) -> str:
        parts = [f"Verdict: {report.verdict.value.upper()}."]
        if report.evaluation:
            met = sum(1 for c in report.evaluation.criteria if c.met is True)
            unver = sum(1 for c in report.evaluation.criteria if c.met is None)
            failed = sum(1 for c in report.evaluation.criteria if c.met is False)
            parts.append(
                f"{met} criteria met, {failed} not met, {unver} unverified "
                f"out of {len(report.evaluation.criteria)}.")
            if report.evaluation.caveats:
                parts.append("Caveats: "
                             + "; ".join(report.evaluation.caveats) + ".")
        silent = [r.step.id for r in report.step_results if r.silent_failure]
        if silent:
            parts.append(
                f"Steps {', '.join(silent)} were accepted by the firmware but "
                f"produced no visible change - input is not reaching xCloud.")
        if report.root_cause:
            parts.append(f"Likely cause "
                         f"({report.root_cause.primary.cause_class.value}): "
                         f"{report.root_cause.primary.statement}")
            if report.root_cause.primary.discriminating_test:
                parts.append("To confirm or rule that out: "
                             + report.root_cause.primary.discriminating_test)
        parts.append("(No LLM was available, so this summary was generated "
                     "mechanically from the recorded results.)")
        return " ".join(parts)

    @staticmethod
    def _recommendations(state: GraphState, report: TestReport) -> list[str]:
        """Deduplicated, ordered: fix the rig, then improve the harness."""
        out: list[str] = []
        if report.root_cause:
            out += report.root_cause.recommendations
            if report.root_cause.primary.discriminating_test:
                out.append("Run the discriminating test before changing "
                           "anything: "
                           + report.root_cause.primary.discriminating_test)
        env = report.environment
        if env is not None and not env.capabilities.can_screenshot:
            out.append(
                "Give the rig EYES. Without adb every verdict is capped at "
                "inconclusive, because a firmware OK cannot prove the app "
                "reacted. The phone's USB port is busy being an OTG host, so "
                "use adb over Wi-Fi: `adb tcpip 5555` then "
                "`adb connect <phone-ip>:5555`.")
        if env is not None and not env.capabilities.can_read_text:
            out.append(
                "Install tesseract (the BINARY, not just pytesseract) to read "
                "on-screen text such as 'Starting your game' or an error code.")
        if report.scenario and report.scenario.ambiguities:
            out.append(
                "Tighten the scenario: " + "; ".join(
                    report.scenario.ambiguities[:3]))
        unchecked = [r.step.id for r in report.step_results
                     if not r.step.expectation]
        if unchecked:
            out.append(
                f"Steps {', '.join(unchecked)} had no expectation, so they "
                f"could not pass or fail. Give each one an observable "
                f"expectation to make the run mean more.")
        # Preserve order, drop duplicates.
        seen: set[str] = set()
        return [r for r in out if not (r in seen or seen.add(r))]

    # -- writing -----------------------------------------------------------
    def _write(self, report: TestReport) -> list[Path]:
        formats = [str(f).lower() for f in
                   self.s.get_list("report.formats", ["json", "markdown"])]
        out_dir = self.s.report_dir()
        stem = f"{report.run_id}_{report.verdict.value}"
        written: list[Path] = []

        if "json" in formats:
            path = out_dir / f"{stem}.json"
            try:
                path.write_text(
                    report.model_dump_json(indent=2), encoding="utf-8")
                written.append(path)
            except OSError as exc:
                self.notes.append(f"could not write {path}: {exc}")

        if "markdown" in formats or "md" in formats:
            path = out_dir / f"{stem}.md"
            try:
                path.write_text(self._markdown(report), encoding="utf-8")
                written.append(path)
            except OSError as exc:
                self.notes.append(f"could not write {path}: {exc}")

        if "html" in formats:
            path = out_dir / f"{stem}.html"
            try:
                path.write_text(self._html(report), encoding="utf-8")
                written.append(path)
            except OSError as exc:
                self.notes.append(f"could not write {path}: {exc}")

        return written

    def _markdown(self, report: TestReport) -> str:
        sections = [str(s) for s in self.s.get_list(
            "report.sections",
            ["verdict", "scenario", "environment", "steps", "evidence",
             "root_cause", "recommendations"])]
        icon = {Verdict.PASS: "PASS", Verdict.FAIL: "FAIL",
                Verdict.BLOCKED: "BLOCKED", Verdict.ERROR: "ERROR",
                Verdict.INCONCLUSIVE: "INCONCLUSIVE"}[report.verdict]

        out = [f"# {icon} - {report.scenario.title if report.scenario else report.run_id}",
               "",
               f"**Run** `{report.run_id}` | **Started** {report.started_at} | "
               f"**Duration** {report.duration_seconds}s | "
               f"**Replans** {report.replans}",
               "",
               "## Summary", "", report.executive_summary, ""]

        if "scenario" in sections and report.scenario:
            out += ["## Scenario", "", f"**Intent.** {report.scenario.intent}", ""]
            if report.scenario.acceptance_criteria:
                out += ["| # | Criterion | Critical | Result |",
                        "|---|-----------|----------|--------|"]
                results = {c.criterion_id: c
                           for c in (report.evaluation.criteria
                                     if report.evaluation else [])}
                for crit in report.scenario.acceptance_criteria:
                    got = results.get(crit.id)
                    word = ({True: "met", False: "NOT met", None: "unverified"}
                            [got.met] if got else "not evaluated")
                    out.append(f"| {crit.id} | {crit.statement} | "
                               f"{'yes' if crit.critical else 'no'} | {word} |")
                out.append("")
            if report.scenario.ambiguities:
                out += ["**Ambiguities noted before running.**", ""]
                out += [f"- {a}" for a in report.scenario.ambiguities] + [""]

        if "environment" in sections and report.environment:
            pad = report.environment.pad
            android = report.environment.android
            out += ["## Environment", "",
                    f"- Pad link: `{pad.port}` open={pad.link_open}, "
                    f"firmware `{pad.firmware}`, transport `{pad.transport}`",
                    f"- Pad enumerated by a host (phone in OTG host mode): "
                    f"**{pad.pad_connected_to_phone}**",
                    f"- Device: {android.model or 'unknown'} "
                    f"(Android {android.android_version or '?'}), "
                    f"adb={android.adb_available}",
                    f"- xCloud is a PWA: launched by URL "
                    f"`{self.s.get('android.pwa.url')}` in "
                    f"`{android.chosen_launcher or 'no browser discovered'}`",
                    ""]
            if report.environment.warnings:
                out += ["**Warnings.**", ""]
                out += [f"- {w}" for w in report.environment.warnings] + [""]

        if "steps" in sections and report.step_results:
            out += ["## Steps", "",
                    "| # | Action | Expectation | HW ok | Met | Notes |",
                    "|---|--------|-------------|-------|-----|-------|"]
            for result in report.step_results:
                step = result.step
                met = {True: "yes", False: "**NO**", None: "unknown"}[
                    result.expectation_met]
                flag = " (SILENT FAILURE)" if result.silent_failure else ""
                action = f"{step.kind.value} {step.target or ''}".strip()
                if step.times > 1:
                    action += f" x{step.times}"
                out.append(
                    f"| {step.id} | {action} | "
                    f"{(step.expectation or '-')[:60]} | "
                    f"{'yes' if result.hardware_ok else 'no'} | {met} | "
                    f"{result.reasoning[:100].replace('|', '/')}{flag} |")
            out.append("")

        if "evidence" in sections and self.s.get("report.include_screenshots",
                                                 True):
            shots = [(r.step.id, r.observation.screenshot_path)
                     for r in report.step_results
                     if r.observation and r.observation.screenshot_path]
            if shots:
                out += ["## Evidence", ""]
                for step_id, path in shots:
                    out.append(f"- `{step_id}`: [{Path(path).name}]({path})")
                out.append("")

        if "root_cause" in sections and report.root_cause:
            rca = report.root_cause
            out += ["## Root cause", "",
                    f"**Layer.** `{rca.layer}`  ",
                    f"**Cause.** `{rca.primary.cause_class.value}` "
                    f"(likelihood {rca.primary.likelihood:.0%}) - "
                    f"{rca.primary.statement}", ""]
            if rca.primary.discriminating_test:
                out += ["**Check that could disprove this.** "
                        + rca.primary.discriminating_test, ""]
            if rca.narrative:
                out += [rca.narrative, ""]
            if rca.alternatives:
                out += ["**Alternatives considered.**", ""]
                out += [f"- `{h.cause_class.value}` ({h.likelihood:.0%}): "
                        f"{h.statement}" for h in rca.alternatives] + [""]

        if "recommendations" in sections and report.recommendations:
            out += ["## Recommendations", ""]
            out += [f"{i}. {r}" for i, r in
                    enumerate(report.recommendations, start=1)] + [""]

        out += ["---", "",
                f"*Generated by Android-Agentic-Xcloud-Testing. "
                f"{len(report.agent_trace)} agent actions recorded; the full "
                f"state is in the JSON report beside this file.*"]
        return "\n".join(out)

    @staticmethod
    def _html(report: TestReport) -> str:
        """Self-contained HTML, with screenshots inline for a quick visual scan."""
        colour = {Verdict.PASS: "#1a7f37", Verdict.FAIL: "#cf222e",
                  Verdict.BLOCKED: "#9a6700", Verdict.ERROR: "#cf222e",
                  Verdict.INCONCLUSIVE: "#57606a"}[report.verdict]
        esc = html.escape

        # Built with concatenation rather than one big f-string: an f-string
        # expression may not contain a backslash, and escaped quotes are
        # unavoidable when emitting inline HTML attributes.
        silent_badge = '<span style="color:#cf222e">SILENT FAILURE</span>'

        rows = []
        for result in report.step_results:
            step = result.step
            met = {True: "yes", False: "NO", None: "unknown"}[
                result.expectation_met]
            shot = ""
            if result.observation and result.observation.screenshot_path:
                url = "file:///" + esc(result.observation.screenshot_path)
                shot = ('<a href="' + url + '"><img src="' + url
                        + '" style="max-width:180px;border:1px solid #ddd"></a>')
            rows.append(
                "<tr><td><code>" + esc(step.id) + "</code></td>"
                + "<td>" + esc(step.kind.value) + " "
                + esc(step.target or "") + "</td>"
                + "<td>" + esc(step.expectation or "-") + "</td>"
                + "<td>" + ("yes" if result.hardware_ok else "no") + "</td>"
                + "<td><b>" + met + "</b>"
                + (" " + silent_badge if result.silent_failure else "") + "</td>"
                + "<td>" + esc(result.reasoning[:300]) + "</td>"
                + "<td>" + shot + "</td></tr>")

        criteria = ""
        if report.evaluation:
            words = {True: "met", False: "NOT met", None: "unverified"}
            criteria = "".join(
                "<li><code>" + esc(c.criterion_id) + "</code> <b>"
                + words[c.met] + "</b> - " + esc(c.statement)
                + "<br><small>" + esc(c.reasoning) + "</small></li>"
                for c in report.evaluation.criteria)

        caveats = ""
        if report.evaluation and report.evaluation.caveats:
            caveats = ("<h2>Caveats</h2><ul>" + "".join(
                f"<li>{esc(c)}</li>" for c in report.evaluation.caveats)
                + "</ul>")

        rca = ""
        if report.root_cause:
            r = report.root_cause
            rca = (f"<h2>Root cause</h2><p><b>Layer:</b> "
                   f"<code>{esc(r.layer)}</code><br>"
                   f"<b>Cause:</b> <code>{esc(r.primary.cause_class.value)}</code>"
                   f" ({r.primary.likelihood:.0%}) - {esc(r.primary.statement)}"
                   f"</p><p><b>Check that could disprove this:</b> "
                   f"{esc(r.primary.discriminating_test)}</p>"
                   f"<p>{esc(r.narrative)}</p>")

        recs = "".join(f"<li>{esc(r)}</li>" for r in report.recommendations)

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{esc(report.run_id)} - {report.verdict.value}</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem auto;
       max-width:1100px;line-height:1.5;color:#1f2328}}
 .verdict{{display:inline-block;padding:.3rem .9rem;border-radius:6px;
          background:{colour};color:#fff;font-weight:700;letter-spacing:.04em}}
 table{{border-collapse:collapse;width:100%;margin:1rem 0;font-size:.9rem}}
 th,td{{border:1px solid #d0d7de;padding:.45rem;text-align:left;
        vertical-align:top}}
 th{{background:#f6f8fa}}
 code{{background:#f6f8fa;padding:.1rem .3rem;border-radius:3px}}
 small{{color:#57606a}}
</style></head><body>
<h1><span class="verdict">{report.verdict.value.upper()}</span>
 {esc(report.scenario.title if report.scenario else report.run_id)}</h1>
<p><small>run <code>{esc(report.run_id)}</code> &middot;
 {esc(report.started_at)} &middot; {report.duration_seconds}s &middot;
 {report.replans} replan(s)</small></p>
<h2>Summary</h2><p>{esc(report.executive_summary)}</p>
<h2>Acceptance criteria</h2><ul>{criteria}</ul>
{caveats}
<h2>Steps</h2>
<table><tr><th>#</th><th>Action</th><th>Expectation</th><th>HW ok</th>
<th>Met</th><th>Reasoning</th><th>Screen</th></tr>{''.join(rows)}</table>
{rca}
<h2>Recommendations</h2><ol>{recs}</ol>
<hr><p><small>Android-Agentic-Xcloud-Testing &middot; xCloud is a PWA, so the
foreground app is a browser by design.</small></p>
</body></html>"""
