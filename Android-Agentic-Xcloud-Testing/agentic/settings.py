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
import re
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "agentic.yaml"
ENV_PREFIX = "XAT_"
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def load_dotenv(path: Path | None = None) -> list[str]:
    """Load KEY=VALUE lines from a .env file into os.environ."""
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
                if key.startswith("export "):
                    key = key[7:].strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded.append(key)
        except OSError:
            continue
    return loaded


def _coerce(text: str) -> Any:
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


def _resolve_env_reference(value: Any) -> Any:
    """Resolve a YAML scalar such as `${ANDROID_SERIAL`}` from os.environ.

    This is intentionally exact-match only. It avoids silently rewriting normal
    strings and makes missing references fall back to the caller's default.
    """
    if not isinstance(value, str):
        return value
    match = _ENV_REF.fullmatch(value.strip())
    if not match:
        return value
    env_name = match.group(1)
    if env_name not in os.environ:
        return None
    return _coerce(os.environ[env_name])


class Settings:
    """Dotted-path, layered view over agentic.yaml."""

    def __init__(self, path: Path | str | None = None,
                 overrides: dict[str, Any] | None = None,
                 use_dotenv: bool = True):
        self.dotenv_keys = load_dotenv() if use_dotenv else []
        self.path = Path(path) if path else DEFAULT_CONFIG
        self.data: dict[str, Any] = {}
        if self.path.is_file():
            with self.path.open("r", encoding="utf-8") as fh:
                self.data = yaml.safe_load(fh) or {}
        self.config_found = self.path.is_file()
        self._overrides: dict[str, Any] = dict(overrides or {})

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

        if node is None:
            return default
        resolved = _resolve_env_reference(node)
        return default if resolved is None else resolved

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted, {})
        return value if isinstance(value, dict) else {}

    def get_list(self, dotted: str, default: list[Any] | None = None) -> list[Any]:
        value = self.get(dotted, None)
        if value is None:
            return list(default or [])
        if isinstance(value, (list, tuple)):
            return list(value)
        return [p.strip() for p in str(value).split(",") if p.strip()]

    def override(self, dotted: str, value: Any) -> None:
        self._overrides[dotted] = value

    def resolve_path(self, dotted: str, default: str) -> Path:
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
