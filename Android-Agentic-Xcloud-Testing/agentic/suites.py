"""
suites.py - discovering and resolving test cases and suites.

Three ways to name what you want to run, in order of convenience:

    --case controller_detected      by id or filename stem
    --suite smoke                   a named group from scenarios/suites.yaml
    "press A and watch the screen"  inline prose, no file at all

WHY A LOADER RATHER THAN A GLOB
-------------------------------
A bare `scenarios/*.yaml` glob would run cases in alphabetical order, and
alphabetical order is actively wrong here: `every_control_responds` would run
before `controller_detected`, so a dead input path would be reported as five
separate mysterious failures instead of one clear "the pad is not reaching
xCloud". Suites exist to encode that dependency, and `stop_on_failure` exists
to act on it.

This module parses only the OUTER wrapper of a case - id, title, tags - and
hands the whole file text to the ScenarioAgent untouched. That distinction
matters: the metadata is for organising runs, and the CONTENT stays free text
with no schema imposed on it. A markdown case with no front matter is equally
valid, and gets sensible defaults derived from its filename.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .settings import PACKAGE_ROOT

SCENARIO_SUFFIXES = (".yaml", ".yml", ".md", ".txt")


@dataclass
class TestCase:
    """One scenario file, plus whatever metadata it declared."""
    id: str
    title: str
    path: Path
    text: str                       # the FULL file, handed to the agent as-is
    tags: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.id or self.path.stem


@dataclass
class Suite:
    name: str
    description: str
    cases: list[TestCase]
    stop_on_failure: bool = False
    missing: list[str] = field(default_factory=list)


class SuiteLoader:
    """Finds cases and suites under scenarios/."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else PACKAGE_ROOT / "scenarios"
        self.cases_dir = self.root / "cases"
        self.suites_file = self.root / "suites.yaml"

    # -- cases -------------------------------------------------------------
    def _search_dirs(self) -> list[Path]:
        """cases/ first, then scenarios/ itself.

        Both are searched so a loose file dropped straight into scenarios/ still
        works - the tidy layout is a convention, not a requirement someone has
        to learn before they can run their first test.
        """
        return [d for d in (self.cases_dir, self.root) if d.is_dir()]

    def load_case(self, path: Path) -> TestCase:
        """Read one case file. Never raises on bad metadata."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return TestCase(id=path.stem, title=f"UNREADABLE: {exc}",
                            path=path, text="")

        case_id, title, tags = path.stem, path.stem.replace("_", " "), []

        # Only YAML declares metadata. Markdown and text cases are pure content,
        # which is the point - the barrier to writing one should be zero.
        if path.suffix.lower() in (".yaml", ".yml"):
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                data = None      # a malformed case is still runnable as prose
            if isinstance(data, dict):
                case_id = str(data.get("id") or case_id)
                title = str(data.get("title") or data.get("name") or title)
                raw_tags = data.get("tags") or []
                if isinstance(raw_tags, (list, tuple)):
                    tags = [str(t) for t in raw_tags]
        elif path.suffix.lower() == ".md":
            # Use the first markdown heading as the title, if there is one.
            for line in text.splitlines():
                if line.strip().startswith("#"):
                    title = line.lstrip("#").strip()
                    break

        return TestCase(id=case_id, title=title, path=path, text=text, tags=tags)

    def all_cases(self) -> list[TestCase]:
        seen: set[Path] = set()
        cases: list[TestCase] = []
        for directory in self._search_dirs():
            for path in sorted(directory.iterdir()):
                if (path.is_file() and path.suffix.lower() in SCENARIO_SUFFIXES
                        and path.name != self.suites_file.name
                        and path.resolve() not in seen):
                    seen.add(path.resolve())
                    cases.append(self.load_case(path))
        return cases

    def find_case(self, wanted: str) -> TestCase | None:
        """Resolve by explicit path, then by id, then by filename stem."""
        direct = Path(wanted)
        if direct.is_file():
            return self.load_case(direct)

        key = wanted.strip().lower().removesuffix(".yaml").removesuffix(".md")
        for case in self.all_cases():
            if key in (case.id.lower(), case.path.stem.lower()):
                return case
        return None

    def find_by_tag(self, tag: str) -> list[TestCase]:
        tag = tag.strip().lower()
        return [c for c in self.all_cases()
                if tag in [t.lower() for t in c.tags]]

    # -- suites ------------------------------------------------------------
    def suite_definitions(self) -> dict[str, Any]:
        if not self.suites_file.is_file():
            return {}
        try:
            data = yaml.safe_load(self.suites_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return {}
        suites = (data or {}).get("suites")
        return suites if isinstance(suites, dict) else {}

    def load_suite(self, name: str) -> Suite | None:
        spec = self.suite_definitions().get(name)
        if not isinstance(spec, dict):
            return None

        cases: list[TestCase] = []
        missing: list[str] = []
        for entry in (spec.get("cases") or []):
            path = self.root / str(entry)
            if path.is_file():
                cases.append(self.load_case(path))
                continue
            # A suite may reference a case by id rather than by path.
            found = self.find_case(str(entry))
            if found is not None:
                cases.append(found)
            else:
                # Recorded, not raised: one bad entry must not stop a suite of
                # ten from running, but it must be visible in the output.
                missing.append(str(entry))

        return Suite(
            name=name,
            description=str(spec.get("description", "")).strip(),
            cases=cases,
            stop_on_failure=bool(spec.get("stop_on_failure", False)),
            missing=missing,
        )

    # -- listing -----------------------------------------------------------
    def describe(self) -> str:
        """Human-readable inventory, for `--list`."""
        lines = [f"scenarios root: {self.root}", ""]

        suites = self.suite_definitions()
        lines.append("SUITES  (python main.py --suite <name>)")
        if not suites:
            lines.append(f"  (none defined - create {self.suites_file.name})")
        for name in suites:
            suite = self.load_suite(name)
            if suite is None:
                continue
            stop = "stops on first failure" if suite.stop_on_failure \
                else "runs every case"
            lines.append(f"  {name:<14} {len(suite.cases)} case(s), {stop}")
            if suite.description:
                lines.append(f"                 {suite.description.splitlines()[0]}")
            for case in suite.cases:
                lines.append(f"                   - {case.name}")
            for miss in suite.missing:
                lines.append(f"                   ! MISSING: {miss}")

        cases = self.all_cases()
        lines += ["", "CASES  (python main.py --case <id>)"]
        if not cases:
            lines.append("  (none found)")
        for case in cases:
            tags = f"  [{', '.join(case.tags)}]" if case.tags else ""
            lines.append(f"  {case.name:<26} {case.title}{tags}")
            lines.append(f"  {'':<26} {case.path.relative_to(self.root)}")
        return "\n".join(lines)
