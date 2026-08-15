"""
settings.py - config access with defaults, so no agent ever KeyErrors.

Every read goes through `Settings.get("a.b.c", default)`. That single rule is
what lets config/agentic.yaml stay optional-by-key: delete a section and you
get the documented default instead of a crash halfway through a hardware run.

Precedence, highest first:
    1. CLI flags        (wired in cli.py -> Settings.override)
    2. environment      (XAT_<PATH_WITH_UNDERSCORES>, e.g. XAT_HARDWARE_DRY_RUN)
    3. config/agentic.yaml
    4. the default passed at the call site

A .env file beside this package (or one level up, next to the .bat files) is
loaded into the environment at construction, which is how API keys arrive
without ever being written into a config file that might get committed.

Nothing here knows anything about xCloud, buttons, or tests. It is plumbing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# This file lives at <root>/agentic/settings.py, so the package root is one up.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "agentic.yaml"

ENV_PREFIX = "XAT_"


def load_dotenv(path: Path | None = None) -> list[str]:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Hand-rolled rather than depending on python-dotenv: it is twenty lines, and
    an API key being unreadable because an optional package is missing is a
    genuinely annoying way to lose a hardware run.

    Existing environment variables WIN. A real exported variable must always
    beat a stale file, or `set OPENAI_API_KEY=...` in a shell would silently do
    nothing and be very hard to explain.

    Searched, nearest first: this package, then the project root above it - so a
    single .env beside the .bat files serves both.
    """
    loaded: list[str] = []
    candidates = [path] if path else [PACKAGE_ROOT / ".env",
                                      PACKAGE_ROOT.parent / ".env"]
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                # `export FOO=bar` is a common paste from shell instructions.
                if key.startswith("export "):
                    key = key[7:].strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded.append(key)
        except OSError:
            # An unreadable .env is not worth failing a run over.
            continue
    return loaded



def _coerce(text: str) -> Any:
    """Turn an env-var string into a bool/int/float/None when it clearly is one.

    Env vars are always strings, but `XAT_HARDWARE_DRY_RUN=true` must become a
    real bool or `if dry_run:` would be true for the string "false" too - a
    genuinely dangerous bug here, because it decides whether we touch hardware.
    """
    low = text.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", ""):
        return None
    try:
        return int(low)
    except ValueError:
        pass
    try:
        return float(low)
    except ValueError:
        pass
    return text


class Settings:
    """Dotted-path, layered view over agentic.yaml."""

    def __init__(self, path: Path | str | None = None,
                 overrides: dict[str, Any] | None = None,
                 use_dotenv: bool = True):
        # Before anything reads os.environ - the LLM factory looks up API keys
        # at construction time, so a late load would be a load that never counted.
        self.dotenv_keys = load_dotenv() if use_dotenv else []

        self.path = Path(path) if path else DEFAULT_CONFIG
        self.data: dict[str, Any] = {}
        if self.path.is_file():
            with self.path.open("r", encoding="utf-8") as fh:
                self.data = yaml.safe_load(fh) or {}
        # A missing config file is not fatal: every getter has a default, so the
        # tool still runs. We record it so the report can say so honestly.
        self.config_found = self.path.is_file()
        self._overrides: dict[str, Any] = dict(overrides or {})

    # -- reading -----------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        if dotted in self._overrides:
            value = self._overrides[dotted]
            if value is not None:
                return value

        env_key = ENV_PREFIX + dotted.replace(".", "_").upper()
        if env_key in os.environ:
            return _coerce(os.environ[env_key])

        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        # An explicit `key: null` in YAML means "unset", so fall back. This is
        # deliberate: the config uses null as "auto-detect" (e.g. serial_port).
        return default if node is None else node

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted, {})
        return value if isinstance(value, dict) else {}

    def get_list(self, dotted: str, default: list[Any] | None = None) -> list[Any]:
        value = self.get(dotted, None)
        if value is None:
            return list(default or [])
        if isinstance(value, (list, tuple)):
            return list(value)
        # Accept a comma-separated string so env-var overrides work for lists.
        return [p.strip() for p in str(value).split(",") if p.strip()]

    # -- writing (CLI flags only) ------------------------------------------
    def override(self, dotted: str, value: Any) -> None:
        self._overrides[dotted] = value

    # -- paths -------------------------------------------------------------
    def resolve_path(self, dotted: str, default: str) -> Path:
        """Resolve a configured path relative to the PACKAGE root.

        Relative-to-package (not to the shell's cwd) matters: the .bat files run
        from the project root while a developer runs python from inside this
        folder, and both must find ../config/controls.yaml.
        """
        raw = str(self.get(dotted, default))
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        return (PACKAGE_ROOT / candidate).resolve()

    def artifact_dir(self, run_id: str, sub: str = "") -> Path:
        base = self.resolve_path("vision.screenshot_dir", "artifacts") / run_id
        target = base / sub if sub else base
        target.mkdir(parents=True, exist_ok=True)
        return target

    def report_dir(self) -> Path:
        target = self.resolve_path("report.output_dir", "reports")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def __repr__(self) -> str:
        return (f"Settings(path={self.path}, found={self.config_found}, "
                f"overrides={len(self._overrides)})")
