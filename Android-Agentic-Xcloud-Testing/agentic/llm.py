"""
llm.py - runtime model resolution and structured calls.

Two jobs:

1. Build a chat model from a NAMED PROFILE in config/agentic.yaml. The provider
   class is imported lazily, inside the branch, so having only
   `langchain-openai` installed does not break a run that uses ollama.

2. `structured()` - ask for a pydantic type and get that type back, with a
   retry and an explicit, reportable failure. An agent must never hand
   half-parsed prose to the next agent.

DEGRADED MODE
-------------
If no model can be built (no key, no package, no server), `LLMFactory.available`
is False and every agent falls back to a deterministic, no-LLM path. The run
still produces a report - it just says which conclusions were mechanical rather
than reasoned. That is the same honesty rule as the parent project: a check that
cannot say "no" is worse than no check.
"""

from __future__ import annotations

import json
import os
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .settings import Settings

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """No usable model. Callers catch this and degrade rather than crash."""


class LLMFactory:
    """Builds and caches chat models per profile name."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._cache: dict[str, Any] = {}
        self.errors: list[str] = []
        self.calls = 0

    # -- profile resolution ------------------------------------------------
    def profile_name_for(self, agent: str) -> str:
        """Per-agent override, else the globally active profile."""
        override = self.s.get(f"llm.agent_profiles.{agent}")
        return str(override or self.s.get("llm.active", "openai"))

    def profile(self, agent: str) -> dict[str, Any]:
        name = self.profile_name_for(agent)
        spec = self.s.section(f"llm.profiles.{name}")
        if not spec:
            # An unknown name is a config typo. Report it rather than silently
            # falling back to some other model - a test run must be able to say
            # which model produced its verdict.
            self.errors.append(
                f"llm profile '{name}' (for agent '{agent}') is not defined in "
                f"llm.profiles - check config/agentic.yaml")
        return {"_name": name, **spec}

    def supports_vision(self, agent: str) -> bool:
        return bool(self.profile(agent).get("supports_vision", False))

    # -- construction ------------------------------------------------------
    def get(self, agent: str) -> Any:
        spec = self.profile(agent)
        name = spec["_name"]
        if name in self._cache:
            return self._cache[name]

        model = self._build(spec)
        self._cache[name] = model
        return model

    def _build(self, spec: dict[str, Any]) -> Any:
        provider = str(spec.get("provider", "")).lower()
        model_name = spec.get("model")
        if not provider or not model_name:
            raise LLMUnavailable(
                f"profile '{spec.get('_name')}' needs both `provider` and "
                f"`model`; got provider={provider!r} model={model_name!r}")

        temperature = float(spec.get("temperature", 0.0))
        key_env = spec.get("api_key_env")
        api_key = os.environ.get(key_env) if key_env else None
        # ollama and other local servers need no key; everything else does.
        if key_env and not api_key and provider not in ("ollama",):
            raise LLMUnavailable(
                f"environment variable {key_env} is not set, so profile "
                f"'{spec.get('_name')}' ({provider}/{model_name}) cannot be used")

        kwargs: dict[str, Any] = {"model": model_name, "temperature": temperature}
        if spec.get("base_url"):
            kwargs["base_url"] = spec["base_url"]

        try:
            if provider in ("openai", "azure_openai"):
                from langchain_openai import ChatOpenAI
                if api_key:
                    kwargs["api_key"] = api_key
                return ChatOpenAI(**kwargs)

            if provider == "anthropic":
                from langchain_anthropic import ChatAnthropic
                if api_key:
                    kwargs["api_key"] = api_key
                return ChatAnthropic(**kwargs)

            if provider in ("google_genai", "gemini", "google"):
                from langchain_google_genai import ChatGoogleGenerativeAI
                if api_key:
                    kwargs["google_api_key"] = api_key
                kwargs.pop("base_url", None)
                return ChatGoogleGenerativeAI(**kwargs)

            if provider == "ollama":
                from langchain_ollama import ChatOllama
                return ChatOllama(**kwargs)

            # Last resort: langchain's own registry, so a provider we never
            # named here still works if the user installs its package.
            from langchain.chat_models import init_chat_model
            return init_chat_model(model_name, model_provider=provider,
                                   temperature=temperature)
        except ImportError as exc:
            raise LLMUnavailable(
                f"provider '{provider}' needs an extra package: {exc}. "
                f"See requirements.txt - uncomment the matching line.") from exc

    @property
    def available(self) -> bool:
        """Can we build the default model at all? Probed once, cheaply."""
        try:
            self.get("default")
            return True
        except (LLMUnavailable, Exception) as exc:   # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            if msg not in self.errors:
                self.errors.append(msg)
            return False

    # -- structured calls --------------------------------------------------
    def structured(self, agent: str, schema: type[T], system: str, user: Any,
                   retries: int = 1) -> T:
        """Call the model and return a validated `schema` instance.

        `user` may be a plain string, or a list of content blocks (text +
        image_url) for a vision call.

        Raises LLMUnavailable on definitive failure - agents catch it and fall
        back to their deterministic path, recording that they did so.
        """
        model = self.get(agent)

        try:
            bound = model.with_structured_output(schema)
        except NotImplementedError as exc:
            raise LLMUnavailable(
                f"model for agent '{agent}' does not support structured "
                f"output: {exc}") from exc

        messages = [("system", system)]
        if isinstance(user, str):
            messages.append(("human", user))
        else:
            # Multimodal: langchain wants a HumanMessage with a content list.
            from langchain_core.messages import HumanMessage
            messages = [("system", system), HumanMessage(content=user)]

        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                self.calls += 1
                result = bound.invoke(messages)
                if isinstance(result, schema):
                    return result
                # Some providers hand back a dict; validate it ourselves rather
                # than trusting the shape.
                if isinstance(result, dict):
                    return schema.model_validate(result)
                if isinstance(result, str):
                    return schema.model_validate(json.loads(result))
                raise ValueError(
                    f"unexpected structured-output type {type(result).__name__}")
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                # A malformed response is worth one more try: temperature 0 does
                # not guarantee valid JSON on the first attempt with every model.
                last = exc
                if attempt < retries:
                    messages.append(
                        ("human",
                         f"Your previous reply did not match the required "
                         f"schema ({exc}). Reply again with valid data only."))
                    continue
            except Exception as exc:                 # noqa: BLE001
                # Network / auth / rate limit. Not worth a blind retry loop
                # here; the caller decides whether to degrade or abort.
                last = exc
                break

        raise LLMUnavailable(
            f"agent '{agent}' could not get a valid {schema.__name__}: {last}")

    def text(self, agent: str, system: str, user: str) -> str:
        """Free-text call, for narrative prose (report summaries)."""
        model = self.get(agent)
        self.calls += 1
        response = model.invoke([("system", system), ("human", user)])
        content = getattr(response, "content", response)
        if isinstance(content, list):
            # Some providers return content blocks even for text-only replies.
            return "\n".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content).strip()
        return str(content).strip()
