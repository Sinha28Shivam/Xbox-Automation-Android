"""
rebuild_reports.py - regenerate the HTML/Markdown of PAST runs from their JSON.

    python rebuild_reports.py

WHY THIS IS POSSIBLE AT ALL
---------------------------
Because the JSON report is the whole graph state, not a summary. reporter.py's
own docstring calls it "the PRIMARY artefact ... so a verdict can be re-examined
without the hardware", and this script is that claim being cashed in: the
rendering was fixed, so every historical run can be re-rendered with working
screenshots without re-running a single test on the phone.

It also serves as the check on the embedding fix. A report is only repaired if
its `<img>` tags come out as `data:` URIs, and the script verifies that rather
than trusting it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentic.agents.reporter import ReporterAgent          # noqa: E402
from agentic.graph import build_context                    # noqa: E402
from agentic.logbook import log                            # noqa: E402
from agentic.schemas import TestReport                     # noqa: E402
from agentic.settings import Settings                      # noqa: E402

settings = Settings()
log.configure(run_id="rebuild", level="warn", file_path=None, colour=False)

reports_dir = settings.report_dir()
jsons = sorted(reports_dir.glob("*.json"))
if not jsons:
    print(f"no JSON reports found in {reports_dir}")
    raise SystemExit(0)

print(f"rebuilding {len(jsons)} report(s) from {reports_dir}\n")
failures = 0

for path in jsons:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        report = TestReport.model_validate(data)
    except Exception as exc:                             # noqa: BLE001
        print(f"  SKIP {path.name}: cannot parse - {exc}")
        failures += 1
        continue

    # A minimal context: the reporter only needs settings for paths, plus
    # `started_at`/`elapsed` which the stored report already carries.
    ctx = build_context(settings, report.run_id)
    agent = ReporterAgent(ctx)

    html_path = reports_dir / f"{report.run_id}_{report.verdict.value}.html"
    try:
        html = agent._html(report)                       # noqa: SLF001
        html_path.write_text(html, encoding="utf-8")
    except Exception as exc:                             # noqa: BLE001
        print(f"  FAIL {path.name}: {type(exc).__name__}: {exc}")
        failures += 1
        continue

    # The check that can say no: are the images actually embedded now?
    embedded = len(re.findall(r'<img src="data:image/', html))
    linked = len(re.findall(r'<img src="file:', html))
    shots = sum(1 for s in report.step_results
                if (s.observation and s.observation.screenshot_path)
                or (s.glance_observation
                    and s.glance_observation.screenshot_path))

    status = "OK  " if embedded and not linked else "WARN"
    if linked:
        failures += 1
    print(f"  {status} {html_path.name}")
    print(f"        {embedded} embedded image(s), {linked} still linked, "
          f"{shots} step(s) had a screenshot, {len(html) // 1024} KB")

print()
if failures:
    print(f"{failures} report(s) could not be fully repaired")
    raise SystemExit(1)
print("every report now embeds its screenshots and will render anywhere.")
