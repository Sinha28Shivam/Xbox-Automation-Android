"""
reporter.py - AGENT 7: assemble the report.

Three formats, three readers:

    JSON      CI, and re-reading a run months later. The PRIMARY artefact: it
              is the entire state - every observation, judgement and agent
              action - so a verdict can be re-examined without the hardware.
              That is the difference between a test report and a claim.
    Markdown  a pull request or a chat message.
    HTML      a human, with the screenshots inline.

THE ORDERING RULE
-----------------
Every format leads with the VERDICT and then, immediately, WHAT WAS NOT PROVEN.
Caveats are the part a reader skips and the part that decides whether the
verdict means anything, so they are placed where skipping is hardest. A report
that buries "we could not actually see the screen" under a green banner is
worse than no report.

Each report also carries a short EVIDENCE QUALITY line - how many criteria were
actually verified, and by which sensors. It is the fastest way to tell a strong
result from a lucky one.

ASCII ONLY, deliberately. No em-dashes, no middle dots. The Windows console is
cp1252 and renders them as a replacement character, so "nicer" typography
actively corrupts the surface most people actually read this on.
"""

from __future__ import annotations

import base64
import html
import io
import time
from pathlib import Path

from ..logbook import log
from ..schemas import TestReport, Verdict
from ..state import GraphState
from .base import Agent

try:
    from PIL import Image
    _PIL = True
except ImportError:                                  # pragma: no cover
    Image = None                                     # type: ignore[assignment]
    _PIL = False


ROLE = """\
You write the executive summary of a test run, for an engineer who was not there.

Structure, in this order:
1. The verdict in one sentence, with its confidence stated plainly.
2. WHAT WAS PROVEN, and - just as prominently - WHAT WAS NOT. Name anything
   assumed, unobserved or unverifiable.
3. If it failed: the root cause, and the single next action to take.
4. If it passed: what would make the result stronger next time.

Rules:
* At most 200 words. A report nobody reads protects nobody.
* Never blur "the firmware accepted the command" with "xCloud reacted". They are
  different claims with different evidence.
* IF ZERO STEPS RAN, do not diagnose the sensors. No screenshots exist because
  nothing was executed, not because capture is broken - the sensors were never
  asked for anything. The cause is whatever HALTED the run, and it is given to
  you; report that instead. Writing "root cause: sensor failure, the screenshot
  mechanism did not run" for a run that halted in planning sends the reader to
  debug a working screenshot pipeline, which is worse than saying nothing.
* Distinguish CANNOT RUN from FAILED. A scenario needing a capability this rig
  does not have (text injection, OCR, adb) is BLOCKED on a precondition. That is
  a fact about the rig, not a defect in xCloud, and the summary must not imply
  the app was tested and found wanting.
* Plain prose. No headings, no bullets, no markdown.
"""


# Verdict -> (label, colour, one-line meaning). Shared by every renderer so the
# three formats cannot drift apart in how they describe an outcome.
VERDICT_STYLE = {
    Verdict.PASS: ("PASS", "#1a7f37",
                   "every critical criterion was met on real evidence"),
    Verdict.FAIL: ("FAIL", "#cf222e",
                   "at least one critical criterion was demonstrably not met"),
    Verdict.BLOCKED: ("BLOCKED", "#9a6700",
                      "the test could not meaningfully start"),
    Verdict.INCONCLUSIVE: ("INCONCLUSIVE", "#57606a",
                           "it ran, but the evidence cannot settle the question"),
    Verdict.ERROR: ("ERROR", "#cf222e", "the harness itself malfunctioned"),
}


class ReporterAgent(Agent):
    name = "reporter"

    def run(self, state: GraphState) -> GraphState:
        evaluation = state.get("evaluation")
        verdict = state.get("verdict") or (
            evaluation.verdict if evaluation else Verdict.INCONCLUSIVE)

        report = TestReport(
            run_id=state.get("run_id", "unknown"),
            started_at=time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(self.ctx.started_at)),
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

        # A silently degraded run looks like a weak result when it is really a
        # fixable misconfiguration. Promote the reason to a caveat.
        if self.llm.errors and report.evaluation is not None:
            for err in self.llm.errors:
                caveat = f"LLM degraded: {err}"
                if caveat not in report.evaluation.caveats:
                    report.evaluation.caveats.append(caveat)

        report.recommendations = self._recommendations(report)
        report.executive_summary = self._summary(report)

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

    # ======================================================================
    # Derived facts, shared by all three formats
    # ======================================================================
    @staticmethod
    def _tally(report: TestReport) -> dict[str, int]:
        crits = report.evaluation.criteria if report.evaluation else []
        steps = report.step_results
        return {
            "criteria": len(crits),
            "met": sum(1 for c in crits if c.met is True),
            "failed": sum(1 for c in crits if c.met is False),
            "unverified": sum(1 for c in crits if c.met is None),
            "steps": len(steps),
            "steps_ok": sum(1 for s in steps if s.expectation_met is True),
            "steps_failed": sum(1 for s in steps if s.expectation_met is False),
            "silent": sum(1 for s in steps if s.silent_failure),
            "screenshots": sum(1 for s in steps
                               if s.observation and s.observation.screenshot_path),
        }

    def _evidence_quality(self, report: TestReport) -> str:
        """One line answering: how much should I trust this verdict?"""
        t = self._tally(report)
        sensors: set[str] = set()
        for step in report.step_results:
            if step.observation:
                sensors.update(step.observation.sensors_used)

        if not sensors:
            # NO STEPS AT ALL is a different thing from BLIND STEPS, and saying
            # "no sensor produced data" for the first one is actively misleading.
            #
            # It happened for real: a run halted in the ScenarioAgent because the
            # scenario needed text entry this device refuses, so zero steps ever
            # executed - and the report announced "Root cause: Sensor failure.
            # The screenshot capture mechanism did not run", sending the reader
            # to debug a screenshot pipeline that was working perfectly. The
            # sensors were never ASKED for anything.
            if not report.step_results:
                return ("NOT APPLICABLE - no step ran, so no sensor was ever "
                        "asked for data. This says NOTHING about whether "
                        "screenshots work; the run ended before execution "
                        "began. Look at the halt reason, not at the sensors.")
            return ("NONE - steps ran but no sensor produced data, so nothing "
                    "on screen was verified. This run cannot support any claim "
                    "about xCloud.")


        verified = t["met"] + t["failed"]
        strength = ("strong" if verified == t["criteria"] and t["criteria"]
                    else "partial" if verified else "weak")
        return (f"{strength} - {verified}/{t['criteria']} criteria resolved on "
                f"evidence, {t['screenshots']} screenshots, sensors: "
                f"{', '.join(sorted(sensors))}")

    # ======================================================================
    # Prose
    # ======================================================================
    def _summary(self, report: TestReport) -> str:
        try:
            text = self.llm.text(self.name, self.system_prompt(ROLE),
                                 self._facts(report))
            self.llm_used = True
            if text:
                return text
        except Exception as exc:                     # noqa: BLE001
            self.notes.append(f"reporter: prose summary unavailable ({exc})")
        return self._mechanical_summary(report)

    def _facts(self, report: TestReport) -> str:
        t = self._tally(report)
        lines = [
            f"VERDICT: {report.verdict.value}",
            f"duration {report.duration_seconds}s, replans {report.replans}",
            f"evidence quality: {self._evidence_quality(report)}",
            f"criteria: {t['met']} met, {t['failed']} not met, "
            f"{t['unverified']} unverified",
            f"steps: {t['steps']} run, {t['silent']} silent failures",
        ]
        if report.scenario:
            lines += [f"scenario: {report.scenario.title}",
                      f"intent: {report.scenario.intent}"]
            if report.scenario.ambiguities:
                lines.append("ambiguities: "
                             + "; ".join(report.scenario.ambiguities))
        if report.environment:
            pad = report.environment.pad
            lines.append(
                f"pad: open={pad.link_open} firmware={pad.firmware} "
                f"host_enumerated={pad.pad_connected_to_phone} "
                f"dry_run={pad.dry_run}")
            if report.environment.warnings:
                lines.append("environment warnings: "
                             + " | ".join(report.environment.warnings))
        if report.evaluation:
            lines.append("criteria detail:")
            for c in report.evaluation.criteria:
                word = {True: "MET", False: "NOT MET", None: "UNVERIFIED"}[c.met]
                lines.append(f"  [{c.criterion_id}] {word}: {c.statement} - "
                             f"{c.reasoning[:180]}")
            if report.evaluation.caveats:
                lines.append("caveats: " + " | ".join(report.evaluation.caveats))
        for step in report.step_results:
            if step.silent_failure:
                lines.append(
                    f"SILENT FAILURE at {step.step.id}: firmware accepted the "
                    f"command, screen did not change")
        if report.root_cause:
            r = report.root_cause
            lines += [f"root cause layer: {r.layer}",
                      f"primary: {r.primary.cause_class.value} - "
                      f"{r.primary.statement}",
                      f"discriminating test: {r.primary.discriminating_test}"]
        return "\n".join(lines)

    def _mechanical_summary(self, report: TestReport) -> str:
        t = self._tally(report)
        label = VERDICT_STYLE[report.verdict][0]
        parts = [f"Verdict: {label}.",
                 f"{t['met']} of {t['criteria']} criteria met, {t['failed']} "
                 f"not met, {t['unverified']} unverified.",
                 f"Evidence quality: {self._evidence_quality(report)}."]
        silent = [s.step.id for s in report.step_results if s.silent_failure]
        if silent:
            parts.append(
                f"At {', '.join(silent)} the firmware accepted the command but "
                f"the screen did not change - input is not reaching xCloud.")
        if report.root_cause:
            parts.append(
                f"Likely cause ({report.root_cause.primary.cause_class.value}): "
                f"{report.root_cause.primary.statement}")
            if report.root_cause.primary.discriminating_test:
                parts.append("To confirm or rule out: "
                             + report.root_cause.primary.discriminating_test)
        parts.append("(Written mechanically - no LLM was available for prose.)")
        return " ".join(parts)

    # ======================================================================
    # Recommendations
    # ======================================================================
    def _recommendations(self, report: TestReport) -> list[str]:
        out: list[str] = []
        if report.root_cause:
            out += report.root_cause.recommendations
            if report.root_cause.primary.discriminating_test:
                out.append("Before changing anything, run the check that could "
                           "disprove the diagnosis: "
                           + report.root_cause.primary.discriminating_test)
        env = report.environment
        if env is not None and not env.capabilities.can_screenshot:
            out.append(
                "Give the rig EYES. Without adb every verdict is capped at "
                "inconclusive, because a firmware OK cannot prove the app "
                "reacted. The phone's USB port is busy being an OTG host, so "
                "use Wi-Fi: `adb tcpip 5555` then `adb connect <phone-ip>:5555`.")
        if env is not None and not env.capabilities.can_read_text:
            out.append(
                "Install the tesseract BINARY (not just pytesseract) to read "
                "on-screen text such as 'Starting your game' or an error code.")
        if self.llm.errors:
            out.append(
                "Fix the LLM configuration - this run reasoned mechanically: "
                + "; ".join(self.llm.errors[:2])
                + ". A 404 naming a model means the id in config/agentic.yaml is "
                "not one your key can see.")
        if report.scenario and report.scenario.ambiguities:
            out.append("Tighten the scenario: "
                       + "; ".join(report.scenario.ambiguities[:3]))
        unchecked = [s.step.id for s in report.step_results
                     if not s.step.expectation]
        if unchecked:
            out.append(
                f"Steps {', '.join(unchecked)} declared no expectation, so they "
                f"could not pass or fail. Give each an observable expectation to "
                f"make the run mean more.")
        seen: set[str] = set()
        return [r for r in out if not (r in seen or seen.add(r))]

    # ======================================================================
    # Writing
    # ======================================================================
    def _write(self, report: TestReport) -> list[Path]:
        formats = [str(f).lower() for f in
                   self.s.get_list("report.formats", ["json", "markdown"])]
        out_dir = self.s.report_dir()
        stem = f"{report.run_id}_{report.verdict.value}"
        written: list[Path] = []

        renderers = [
            ("json", ".json", lambda: report.model_dump_json(indent=2)),
            ("markdown", ".md", lambda: self._markdown(report)),
            ("html", ".html", lambda: self._html(report)),
        ]
        for key, suffix, render in renderers:
            if key not in formats and not (key == "markdown" and "md" in formats):
                continue
            path = out_dir / f"{stem}{suffix}"
            try:
                path.write_text(render(), encoding="utf-8")
                written.append(path)
            except (OSError, ValueError) as exc:
                self.notes.append(f"could not write {path}: {exc}")
        return written

    # ----------------------------------------------------------------------
    # Markdown
    # ----------------------------------------------------------------------
    def _markdown(self, report: TestReport) -> str:
        label, _, meaning = VERDICT_STYLE[report.verdict]
        t = self._tally(report)
        title = report.scenario.title if report.scenario else report.run_id

        out = [
            f"# {label} - {title}",
            "",
            f"> {meaning}",
            "",
            "| | |",
            "|---|---|",
            f"| **Verdict** | `{label}` |",
            f"| **Evidence quality** | {self._evidence_quality(report)} |",
            f"| **Criteria** | {t['met']} met | {t['failed']} not met | "
            f"{t['unverified']} unverified |",
            f"| **Steps** | {t['steps']} run | {t['silent']} silent failure(s) |",
            f"| **Run** | `{report.run_id}` |",
            f"| **When** | {report.started_at} ({report.duration_seconds}s) |",
            f"| **Replans** | {report.replans} |",
            "",
        ]

        # Caveats first. They decide whether the verdict above means anything.
        if report.evaluation and report.evaluation.caveats:
            out += ["## What this run does NOT prove", ""]
            out += [f"- {c}" for c in report.evaluation.caveats] + [""]

        if t["silent"]:
            offenders = ", ".join(f"`{s.step.id}`" for s in report.step_results
                                  if s.silent_failure)
            out += [
                "## Silent failure detected", "",
                f"At {offenders} the firmware **accepted the command** and the "
                f"**screen did not change**.", "",
                "This is the failure a firmware `OK` cannot report: the HID "
                "report was queued, and xCloud did not react.", ""]

        out += ["## Summary", "", report.executive_summary, ""]

        if report.scenario:
            out += ["## Scenario", "",
                    f"**Intent.** {report.scenario.intent}", ""]
            if report.scenario.acceptance_criteria:
                results = {c.criterion_id: c for c in
                           (report.evaluation.criteria if report.evaluation
                            else [])}
                out += ["| ID | Criterion | Critical | Result | Why |",
                        "|---|---|---|---|---|"]
                for crit in report.scenario.acceptance_criteria:
                    got = results.get(crit.id)
                    word = ({True: "met", False: "**NOT met**",
                             None: "unverified"}[got.met]
                            if got else "not evaluated")
                    why = (got.reasoning[:120].replace("|", "/")
                           if got and got.reasoning else "")
                    out.append(
                        f"| `{crit.id}` | {crit.statement} | "
                        f"{'yes' if crit.critical else 'no'} | {word} | {why} |")
                out.append("")

        if report.step_results:
            # Both change ratios are shown side by side, because the WHOLE point
            # of the two-look cycle is that the numbers can disagree - and when
            # they do, the reader must be able to see it without opening the JSON.
            out += ["## Steps", "",
                    "| # | Action | Expectation | HW | Met | Glance | Settled | "
                    "Reacted | Waited |",
                    "|---|---|---|---|---|---|---|---|---|"]
            for r in report.step_results:
                step = r.step
                met = {True: "yes", False: "**NO**", None: "?"}[r.expectation_met]
                action = f"`{step.kind.value}` {step.target or ''}".strip()
                if step.times > 1:
                    action += f" x{step.times}"

                def ratio(o: object) -> str:
                    if o is None or getattr(o, "change_ratio", None) is None:
                        return "-"
                    return f"{o.change_ratio:.2%}"       # type: ignore[union-attr]

                reacted = r.reacted_on
                if r.silent_failure:
                    reacted = "**SILENT FAILURE**"
                elif r.reacted_on == "glance":
                    # Flagged because under the old single-look harness this step
                    # would have been recorded as a failure.
                    reacted = "glance only *(transient)*"
                out.append(
                    f"| `{step.id}` | {action} | "
                    f"{(step.expectation or '-')[:60]} | "
                    f"{'ok' if r.hardware_ok else 'no'} | {met} | "
                    f"{ratio(r.glance_observation)} | "
                    f"{ratio(r.observation)} | {reacted} | "
                    f"{r.waited_seconds:.1f}s |")
            out.append("")

            transient = [r for r in report.step_results
                         if r.reacted_on == "glance"]
            if transient:
                out += [
                    "> **Read this before doubting the hardware.** "
                    + ", ".join(f"`{r.step.id}`" for r in transient)
                    + " reacted ONLY in the glance frame, taken moments after "
                      "the input, and had settled back by the time the second "
                      "frame was captured. The input DID reach xCloud. A harness "
                      "that looked once, later, would have reported these as "
                      "failures - which is exactly what happened in run "
                      "20260817-105323 before the two-look cycle existed.",
                    ""]


        shots = [(r.step.id, r.observation.screenshot_path)
                 for r in report.step_results
                 if r.observation and r.observation.screenshot_path]
        if shots and self.s.get("report.include_screenshots", True):
            out += ["## Evidence", ""]
            out += [f"- `{sid}` - [{Path(p).name}]({Path(p).as_uri()})"
                    for sid, p in shots] + [""]

        if report.root_cause:
            r = report.root_cause
            out += ["## Root cause", "",
                    f"**Layer:** `{r.layer}` | **Cause:** "
                    f"`{r.primary.cause_class.value}` "
                    f"({r.primary.likelihood:.0%} likely)", "",
                    f"{r.primary.statement}", ""]
            if r.primary.discriminating_test:
                out += ["**A check that could disprove this:**", "",
                        f"> {r.primary.discriminating_test}", ""]
            if r.narrative:
                out += [r.narrative, ""]
            if r.alternatives:
                out += ["<details><summary>Alternatives considered</summary>",
                        ""]
                out += [f"- `{h.cause_class.value}` ({h.likelihood:.0%}): "
                        f"{h.statement}" for h in r.alternatives]
                out += ["", "</details>", ""]

        if report.recommendations:
            out += ["## Next actions", ""]
            out += [f"{i}. {rec}"
                    for i, rec in enumerate(report.recommendations, 1)] + [""]

        out += ["---", "",
                f"*{len(report.agent_trace)} agent actions recorded. The full "
                f"state - every observation and judgement - is in "
                f"`{report.run_id}_{report.verdict.value}.json`.*", "",
                "*xCloud is a PWA, so a browser in the foreground is correct, "
                "not a defect.*"]
        return "\n".join(out)

    # ----------------------------------------------------------------------
    # Screenshot embedding
    # ----------------------------------------------------------------------
    def _thumb_data_uri(self, path: str) -> str | None:
        """Read a screenshot and return an inline `data:` URI. None on failure.

        WHY EMBED INSTEAD OF LINKING
        ---------------------------
        The report previously used `<img src="file:///C:/...">`. Those tags were
        correct and the PNGs existed on disk, but they still rendered as nothing,
        because a `file://` SUBRESOURCE is blocked in every situation that
        matters:

          * opened through any http server or preview pane - a page on `http://`
            may not pull in `file://` content (mixed local/remote origin)
          * a VS Code / IDE webview - `file://` is outside the webview's allowed
            resource roots
          * emailed, uploaded to a PR, or copied to another machine - the
            absolute path simply does not exist there

        So the screenshots were "not there in web reports" even though they were
        captured, which is the same failure mode as the timing bug in miniature:
        the evidence existed and the report could not show it.

        Embedding makes the HTML a SINGLE self-contained artefact that renders
        anywhere. The images are downscaled hard first - they are thumbnails in
        the table, and a full 1080p PNG per frame would produce a 40 MB report
        that no browser opens happily. The full-resolution file stays on disk and
        is still linked, so nothing is lost.
        """
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            log.warn(f"cannot embed {path} in the HTML report: {exc}")
            return None

        width = int(self.s.get("report.thumbnail_width", 360))
        if not _PIL or width <= 0:
            # No Pillow: embed the original bytes rather than showing nothing.
            # Honest but potentially large, so say so once.
            if not _PIL:
                log.debug("pillow missing: screenshots are embedded at full "
                          "size, so the HTML report will be large")
            return ("data:image/png;base64,"
                    + base64.b64encode(raw).decode("ascii"))
        try:
            with Image.open(io.BytesIO(raw)) as img:
                img = img.convert("RGB")
                if img.width > width:
                    ratio = width / float(img.width)
                    img = img.resize((width, max(1, int(img.height * ratio))))
                buf = io.BytesIO()
                img.save(buf, format="JPEG",
                         quality=int(self.s.get("report.thumbnail_quality", 72)))
            return ("data:image/jpeg;base64,"
                    + base64.b64encode(buf.getvalue()).decode("ascii"))
        except Exception as exc:                     # noqa: BLE001
            log.warn(f"cannot downscale {path} for the report: {exc}")
            return ("data:image/png;base64,"
                    + base64.b64encode(raw).decode("ascii"))

    # ----------------------------------------------------------------------
    # HTML
    # ----------------------------------------------------------------------
    def _html(self, report: TestReport) -> str:
        esc = html.escape

        label, colour, meaning = VERDICT_STYLE[report.verdict]
        t = self._tally(report)
        title = report.scenario.title if report.scenario else report.run_id

        def card(value: object, caption: str, tone: str = "") -> str:
            style = f' style="color:{tone}"' if tone else ""
            return (f'<div class="card"><div class="num"{style}>{value}</div>'
                    f'<div class="cap">{esc(caption)}</div></div>')

        cards = (
            card(t["met"], "criteria met", "#1a7f37")
            + card(t["failed"], "not met", "#cf222e" if t["failed"] else "")
            + card(t["unverified"], "unverified", "#9a6700" if t["unverified"] else "")
            + card(t["steps"], "steps run")
            + card(t["silent"], "silent failures", "#cf222e" if t["silent"] else "")
            + card(f'{report.duration_seconds:.0f}s', "duration")
        )

        # Caveats, placed directly under the banner - the reader must pass them.
        caveats = ""
        if report.evaluation and report.evaluation.caveats:
            items = "".join(f"<li>{esc(c)}</li>"
                            for c in report.evaluation.caveats)
            caveats = (f'<div class="warn"><h2>What this run does NOT prove</h2>'
                       f'<ul>{items}</ul></div>')

        silent_block = ""
        if t["silent"]:
            ids = ", ".join(esc(s.step.id) for s in report.step_results
                            if s.silent_failure)
            silent_block = (
                f'<div class="danger"><h2>Silent failure</h2><p>At <code>{ids}'
                f'</code> the firmware <b>accepted the command</b> and the '
                f'<b>screen did not change</b>. This is precisely the failure a '
                f'firmware <code>OK</code> cannot report: the HID report was '
                f'queued, and xCloud did not react.</p></div>')

        crit_rows = ""
        if report.evaluation:
            words = {True: ('met', '#1a7f37'), False: ('NOT met', '#cf222e'),
                     None: ('unverified', '#9a6700')}
            for c in report.evaluation.criteria:
                word, tone = words[c.met]
                crit_rows += (
                    f'<tr><td><code>{esc(c.criterion_id)}</code></td>'
                    f'<td>{esc(c.statement)}</td>'
                    f'<td style="color:{tone};font-weight:600">{word}</td>'
                    f'<td>{c.confidence:.0%}</td>'
                    f'<td class="muted">{esc(c.reasoning)}</td></tr>')

        # Both frames are rendered, captioned with their change ratio. Seeing the
        # pair is the fastest possible way to settle "did the app react" - a
        # reader can compare the two images directly instead of trusting a number.
        step_rows = ""
        for r in report.step_results:
            step = r.step
            word, tone = {True: ("yes", "#1a7f37"), False: ("NO", "#cf222e"),
                          None: ("?", "#9a6700")}[r.expectation_met]

            def frame(obs: object, caption: str) -> str:
                path = getattr(obs, "screenshot_path", None) if obs else None
                if not path:
                    # Say so, rather than leaving an empty cell. A missing frame
                    # is a finding: it means this step could not be judged from
                    # pixels, and silence would hide that.
                    return (f'<figure><div class="noshot">no {caption}<br>'
                            f'frame</div></figure>')
                ratio = getattr(obs, "change_ratio", None)
                pct = f"{ratio:.2%}" if ratio is not None else "not measured"
                embedded = self._thumb_data_uri(path)
                if embedded is None:
                    return (f'<figure><div class="noshot">{caption}<br>'
                            f'unreadable</div>'
                            f'<figcaption>{pct}</figcaption></figure>')
                # The thumbnail is embedded so it renders anywhere; the href
                # still points at the full-resolution PNG on disk for anyone
                # reading the report on the machine that produced it.
                return (f'<figure><a href="{Path(path).as_uri()}" '
                        f'target="_blank" title="{esc(Path(path).name)}">'
                        f'<img src="{embedded}" alt="{esc(caption)} frame"></a>'
                        f'<figcaption>{caption}<br>{pct}</figcaption></figure>')

            shots = (frame(r.glance_observation, "glance")
                     + frame(r.observation, "settled"))


            badge = ('<span class="badge">SILENT FAILURE</span>'
                     if r.silent_failure else "")
            if not r.silent_failure and r.reacted_on == "glance":
                # The case the old harness got wrong. Marked so nobody re-opens
                # the same investigation into hardware that is working.
                badge = ('<span class="badge transient">TRANSIENT - input DID '
                         'arrive</span>')

            meta = (f'<div class="muted">reacted_on={esc(r.reacted_on)}'
                    f' &middot; waited {r.waited_seconds:.1f}s'
                    + (f' &middot; {esc(r.settle_profile)}'
                       if r.settle_profile else "")
                    + '</div>')
            step_rows += (
                f'<tr><td><code>{esc(step.id)}</code></td>'
                f'<td><b>{esc(step.kind.value)}</b> {esc(step.target or "")}</td>'
                f'<td>{esc(step.expectation or "-")}</td>'
                f'<td>{"ok" if r.hardware_ok else "no"}</td>'
                f'<td style="color:{tone};font-weight:600">{word}{badge}</td>'
                f'<td class="muted">{esc(r.reasoning[:280])}{meta}</td>'
                f'<td><div class="frames">{shots}</div></td></tr>')


        rca_block = ""
        if report.root_cause:
            r = report.root_cause
            alts = "".join(
                f'<li><code>{esc(h.cause_class.value)}</code> '
                f'({h.likelihood:.0%}): {esc(h.statement)}</li>'
                for h in r.alternatives)
            rca_block = (
                f'<h2>Root cause</h2>'
                f'<p><b>Layer:</b> <code>{esc(r.layer)}</code> &middot; '
                f'<b>Cause:</b> <code>{esc(r.primary.cause_class.value)}</code> '
                f'({r.primary.likelihood:.0%} likely)</p>'
                f'<p>{esc(r.primary.statement)}</p>'
                + (f'<div class="test"><b>A check that could disprove this:</b>'
                   f'<br>{esc(r.primary.discriminating_test)}</div>'
                   if r.primary.discriminating_test else "")
                + (f'<p class="muted">{esc(r.narrative)}</p>' if r.narrative else "")
                + (f'<details><summary>Alternatives considered</summary>'
                   f'<ul>{alts}</ul></details>' if alts else ""))

        env_block = ""
        if report.environment:
            pad = report.environment.pad
            android = report.environment.android
            warns = "".join(f"<li>{esc(w)}</li>"
                            for w in report.environment.warnings)
            env_block = (
                f'<h2>Environment</h2><table>'
                f'<tr><th>Pad link</th><td><code>{esc(str(pad.port))}</code> '
                f'open={pad.link_open}, firmware '
                f'<code>{esc(str(pad.firmware))}</code></td></tr>'
                f'<tr><th>Host enumerated the pad</th>'
                f'<td><b>{pad.pad_connected_to_phone}</b> '
                f'<span class="muted">(phone in OTG host mode)</span></td></tr>'
                f'<tr><th>Device</th><td>{esc(str(android.model))} '
                f'(Android {esc(str(android.android_version))}), '
                f'adb={android.adb_available}</td></tr>'
                f'<tr><th>xCloud PWA</th><td>opened by URL in '
                f'<code>{esc(str(android.chosen_launcher))}</code> '
                f'<span class="muted">- no app package exists</span></td></tr>'
                f'</table>'
                + (f'<div class="warn"><b>Warnings</b><ul>{warns}</ul></div>'
                   if warns else ""))

        recs = "".join(f"<li>{esc(r)}</li>" for r in report.recommendations)

        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(label)} - {esc(title)}</title>
<style>
 :root{{--line:#d0d7de;--bg:#f6f8fa;--muted:#57606a}}
 *{{box-sizing:border-box}}
 body{{font-family:-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;
   padding:2rem 1rem;line-height:1.55;color:#1f2328;background:#fff}}
 .wrap{{max-width:1150px;margin:0 auto}}
 .banner{{background:{colour};color:#fff;padding:1.5rem 1.75rem;
   border-radius:10px;margin-bottom:.5rem}}
 .banner h1{{margin:0 0 .3rem;font-size:1.55rem}}
 .banner .v{{font-size:.8rem;letter-spacing:.18em;opacity:.9;
   text-transform:uppercase;font-weight:700}}
 .banner p{{margin:.4rem 0 0;opacity:.95}}
 .meta{{color:var(--muted);font-size:.85rem;margin:.6rem 0 1.5rem}}
 .cards{{display:flex;flex-wrap:wrap;gap:.75rem;margin-bottom:1.5rem}}
 .card{{flex:1 1 120px;border:1px solid var(--line);border-radius:8px;
   padding:.85rem;text-align:center;background:var(--bg)}}
 .num{{font-size:1.7rem;font-weight:700;line-height:1}}
 .cap{{font-size:.72rem;color:var(--muted);text-transform:uppercase;
   letter-spacing:.06em;margin-top:.3rem}}
 .quality{{border-left:4px solid {colour};background:var(--bg);
   padding:.75rem 1rem;border-radius:0 6px 6px 0;margin-bottom:1.5rem}}
 h2{{font-size:1.15rem;margin:1.8rem 0 .7rem;padding-bottom:.35rem;
   border-bottom:1px solid var(--line)}}
 table{{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;
   font-size:.88rem}}
 th,td{{border:1px solid var(--line);padding:.5rem .6rem;text-align:left;
   vertical-align:top}}
 th{{background:var(--bg);font-weight:600}}
 code{{background:var(--bg);padding:.1rem .35rem;border-radius:4px;
   font-size:.9em}}
 img{{max-width:150px;border:1px solid var(--line);border-radius:4px;
   display:block}}
 .muted{{color:var(--muted);font-size:.9em}}
 .warn{{background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;
   padding:.5rem 1.25rem;margin:1.25rem 0}}
 .warn h2{{border:0;margin:.6rem 0 .3rem}}
 .danger{{background:#ffebe9;border:1px solid #cf222e;border-radius:8px;
   padding:.5rem 1.25rem;margin:1.25rem 0}}
 .danger h2{{border:0;color:#cf222e;margin:.6rem 0 .3rem}}
 .test{{background:#ddf4ff;border-left:4px solid #0969da;padding:.75rem 1rem;
   border-radius:0 6px 6px 0;margin:.75rem 0}}
 .badge{{background:#cf222e;color:#fff;font-size:.65rem;padding:.1rem .4rem;
   border-radius:3px;margin-left:.4rem;white-space:nowrap}}
 .badge.transient{{background:#0969da}}
 .frames{{display:flex;gap:.4rem}}
 figure{{margin:0}}
 figcaption{{font-size:.65rem;color:var(--muted);text-align:center;
   margin-top:.2rem;line-height:1.3}}
 .noshot{{width:110px;height:150px;border:1px dashed var(--line);
   border-radius:4px;display:flex;align-items:center;justify-content:center;
   text-align:center;font-size:.62rem;color:var(--muted);background:var(--bg)}}


 footer{{margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--line);
   color:var(--muted);font-size:.82rem}}
 details{{margin:.5rem 0}} summary{{cursor:pointer;font-weight:600}}
</style></head><body><div class="wrap">

<div class="banner">
  <div class="v">{esc(label)}</div>
  <h1>{esc(title)}</h1>
  <p>{esc(meaning)}</p>
</div>
<div class="meta">run <code>{esc(report.run_id)}</code> &middot;
 {esc(report.started_at)} &middot; {report.duration_seconds}s &middot;
 {report.replans} replan(s)</div>

<div class="cards">{cards}</div>

<div class="quality"><b>Evidence quality:</b>
 {esc(self._evidence_quality(report))}</div>

{caveats}
{silent_block}

<h2>Summary</h2>
<p>{esc(report.executive_summary)}</p>

<h2>Acceptance criteria</h2>
<table><tr><th>ID</th><th>Criterion</th><th>Result</th><th>Conf.</th>
<th>Reasoning</th></tr>{crit_rows}</table>

<h2>Steps</h2>
<table><tr><th>#</th><th>Action</th><th>Expectation</th><th>HW</th>
<th>Met</th><th>Judgement</th><th>Screen</th></tr>{step_rows}</table>

{env_block}
{rca_block}

<h2>Next actions</h2>
<ol>{recs}</ol>

<footer>
{len(report.agent_trace)} agent actions recorded. The full state - every
observation and judgement - is in
<code>{esc(report.run_id)}_{esc(report.verdict.value)}.json</code>.<br>
xCloud is a PWA, so a browser in the foreground is correct, not a defect.
A firmware <code>OK</code> means the HID report was queued; only the screen
can show that the app reacted.
</footer>
</div></body></html>"""
