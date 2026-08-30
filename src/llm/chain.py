"""Ordered multi-provider LLM fallback chain (OpenAI-compatible HTTP).

Portable copy used by the llm-chain skill and installable into any project.
Last step is always ``template`` (caller supplies heuristic / template).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TEMPLATE_SENTINEL = "__TEMPLATE__"

_KNOWN_PROVIDERS = {
    "gemini",
    "groq",
    "openrouter",
    "openai",
    "openai_compatible",
    "compatible",
    "ollama",
    "template",
    "heuristic",
    "default",
    "local",
}

_PROVIDERS_PATH = Path(__file__).resolve().parent / "providers.json"


def _load_providers_catalog() -> dict[str, Any]:
    if _PROVIDERS_PATH.is_file():
        try:
            return json.loads(_PROVIDERS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _env(*names: str, default: str = "") -> str:
    for name in names:
        val = (os.getenv(name) or "").strip()
        if val:
            return val
    return default


def _split_csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _parse_endpoint_spec(spec: str) -> tuple[str, str]:
    s = (spec or "").strip()
    if not s:
        return "template", "template"
    lower = s.lower()
    if lower in {"template", "heuristic", "default", "local"}:
        return "template", "template"
    if "/" in s:
        provider, _, model = s.partition("/")
        # openrouter models are provider/org/model — only split first slash if
        # left side is a known provider name
        if provider.strip().lower() in _KNOWN_PROVIDERS:
            return provider.strip().lower(), (model.strip() or "")
        # otherwise treat whole string as openrouter-style model under openrouter
        # only when it looks like org/model
        return "openrouter", s
    if ":" in s:
        left, _, right = s.partition(":")
        if left.strip().lower() in _KNOWN_PROVIDERS:
            return left.strip().lower(), right.strip()
    return s.lower(), ""


def default_model(provider: str) -> str:
    catalog = _load_providers_catalog()
    providers = catalog.get("providers") or {}
    entry = providers.get(provider) or {}
    if entry.get("default_model"):
        return str(entry["default_model"])
    if provider == "gemini":
        return "gemini-3.5-flash"
    if provider == "groq":
        # Groq shut down llama-3.1-8b-instant / llama-3.3-70b-versatile (2026-08-16).
        return "openai/gpt-oss-120b"
    if provider == "openrouter":
        return "google/gemma-4-26b-a4b-it:free"
    if provider in {"openai", "openai_compatible", "compatible"}:
        return "gpt-4o-mini"
    if provider == "ollama":
        return _env("OLLAMA_MODEL", default="qwen3:8b") or "qwen3:8b"
    return "template"


# Shared quality-first order (best model first). Overridden by env LLM_CHAIN.
DEFAULT_LLM_CHAIN: tuple[tuple[str, str], ...] = (
    ("gemini", "gemini-3.5-flash"),
    ("groq", "openai/gpt-oss-120b"),
    ("gemini", "gemini-flash-latest"),
    ("gemini", "gemini-3.5-flash-lite"),
    ("groq", "openai/gpt-oss-20b"),
    ("gemini", "gemini-flash-lite-latest"),
    ("openrouter", "google/gemma-4-26b-a4b-it:free"),
)


# Free/dev-tier Groq IDs shut down 2026-08-16 → official replacements.
_GROQ_MODEL_ALIASES = {
    "llama-3.1-8b-instant": "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
    "llama3-8b-8192": "openai/gpt-oss-20b",
    "llama3-70b-8192": "openai/gpt-oss-120b",
    "qwen/qwen3-32b": "openai/gpt-oss-120b",
}


def _is_gemini_thinking_model(model: str) -> bool:
    """Gemini 2.5+/3.x Flash and Pro think by default; Lite does not.

    ``gemini-flash-latest`` is a moving alias (now 3.x Flash with thinking on).
    """
    model_l = (model or "").lower()
    if not model_l or "lite" in model_l:
        return False
    if "flash-latest" in model_l:
        return True
    if "gemini-pro" in model_l or "-pro-" in model_l or model_l.endswith("-pro"):
        return True
    if "flash" not in model_l:
        return False
    return any(tag in model_l for tag in ("2.5", "3.5", "3.6", "gemini-3", "3-flash"))


def _canonicalize_model(provider: str, model: str) -> str:
    model = (model or "").strip()
    if provider != "groq" or not model:
        return model
    catalog = _load_providers_catalog()
    aliases = ((catalog.get("providers") or {}).get("groq") or {}).get(
        "deprecated_aliases"
    ) or {}
    merged = {**_GROQ_MODEL_ALIASES, **{str(k): str(v) for k, v in aliases.items()}}
    return merged.get(model, model)


def parse_llm_chain_specs(*, yaml_model: str | None = None) -> list[tuple[str, str]]:
    """
    Parse LLM chain env. Always ends with ``template``.

    Priority:
    1. ``LLM_CHAIN`` CSV
    2. ``LLM_PROVIDER`` / ``LLM_MODEL`` + ``LLM_FALLBACKS``
    3. ``providers.json`` defaults.chain / ``DEFAULT_LLM_CHAIN``
    """
    del yaml_model  # kept for call-site compatibility; order is env/defaults only
    chain_raw = _env("LLM_CHAIN")
    if chain_raw:
        specs = [_parse_endpoint_spec(s) for s in _split_csv(chain_raw)]
    elif _env("LLM_PROVIDER") or _env("LLM_MODEL") or _env("LLM_FALLBACKS"):
        primary_provider = (_env("LLM_PROVIDER") or "gemini").lower()
        primary_model = _env("LLM_MODEL") or default_model(primary_provider)
        specs = [(primary_provider, primary_model)]
        for item in _split_csv(_env("LLM_FALLBACKS")):
            specs.append(_parse_endpoint_spec(item))
    else:
        catalog = _load_providers_catalog()
        defaults = (catalog.get("defaults") or {}).get("chain") or []
        if defaults:
            specs = [_parse_endpoint_spec(str(item)) for item in defaults]
        else:
            specs = list(DEFAULT_LLM_CHAIN)

    remote: list[tuple[str, str]] = []
    ollama_model = default_model("ollama")
    include_ollama = bool(_env("OLLAMA_BASE_URL") or _env("OLLAMA_MODEL"))

    for provider, model in specs:
        provider = provider.lower().strip()
        if provider in {"template", "heuristic", "default", "local"}:
            continue
        if provider == "ollama":
            include_ollama = True
            if (model or "").strip():
                ollama_model = model.strip()
            continue
        model = _canonicalize_model(
            provider, (model or default_model(provider)).strip()
        )
        if remote and remote[-1] == (provider, model):
            continue
        remote.append((provider, model))

    out = list(remote)
    if include_ollama:
        out.append(("ollama", ollama_model))
    out.append(("template", "template"))
    return out


@dataclass
class LLMEndpoint:
    provider: str
    model: str
    base_url: str = ""
    api_key: str = ""

    @property
    def label(self) -> str:
        if self.provider == "template":
            return "template"
        return f"{self.provider}/{self.model or 'default'}"


class LLMChain:
    """Try cloud/local chat models in order; template sentinel on total failure."""

    def __init__(
        self,
        *,
        yaml_model: str | None = None,
        app_title: str = "llm-chain",
        app_referer: str = "https://github.com/llm-chain",
    ) -> None:
        self.app_title = app_title
        self.app_referer = app_referer
        self.chain: list[LLMEndpoint] = self._build_chain(yaml_model=yaml_model)
        self.last_endpoint: str | None = None
        self.last_error: str | None = None
        self.last_used_template = False
        logger.info("LLM chain: %s", " -> ".join(e.label for e in self.chain))

    def _provider_config(self, provider: str) -> tuple[str, str, str]:
        if provider == "gemini":
            return (
                default_model(provider),
                "https://generativelanguage.googleapis.com/v1beta/openai",
                _env("GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY"),
            )
        if provider == "groq":
            return (
                default_model(provider),
                "https://api.groq.com/openai/v1",
                _env("GROQ_API_KEY", "LLM_API_KEY"),
            )
        if provider == "openrouter":
            return (
                default_model(provider),
                "https://openrouter.ai/api/v1",
                _env("OPENROUTER_API_KEY", "LLM_API_KEY"),
            )
        if provider in {"openai", "openai_compatible", "compatible"}:
            return (
                default_model(provider),
                _env("LLM_BASE_URL") or "https://api.openai.com/v1",
                _env("OPENAI_API_KEY", "LLM_API_KEY"),
            )
        if provider == "ollama":
            return (
                default_model(provider),
                (_env("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
                + "/v1",
                _env("LLM_API_KEY") or "ollama",
            )
        return ("template", "", "")

    def _build_chain(self, *, yaml_model: str | None) -> list[LLMEndpoint]:
        endpoints: list[LLMEndpoint] = []
        for provider, model in parse_llm_chain_specs(yaml_model=yaml_model):
            if provider == "template":
                endpoints.append(LLMEndpoint("template", "template"))
                continue
            model_default, base_url, api_key = self._provider_config(provider)
            model = (model or model_default).strip()
            if provider != "ollama" and not api_key:
                logger.warning("Skip %s/%s — no API key", provider, model)
                continue
            endpoints.append(
                LLMEndpoint(
                    provider=provider,
                    model=model,
                    base_url=base_url.rstrip("/"),
                    api_key=api_key,
                )
            )
        if not endpoints or endpoints[-1].provider != "template":
            endpoints.append(LLMEndpoint("template", "template"))
        return endpoints

    def _invoke_endpoint(
        self,
        endpoint: LLMEndpoint,
        system: str,
        user: str,
        *,
        temperature: float = 0.55,
        max_tokens: int = 1800,
    ) -> str:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("httpx is required: pip install httpx") from exc

        url = f"{endpoint.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {endpoint.api_key}",
            "Content-Type": "application/json",
        }
        if endpoint.provider == "openrouter":
            headers["HTTP-Referer"] = self.app_referer
            headers["X-Title"] = self.app_title
        # gpt-oss / Gemini 2.5+/3.x Flash+Pro (incl. flash-latest) burn budget on thinking.
        model_l = (endpoint.model or "").lower()
        is_gpt_oss = "gpt-oss" in model_l
        is_gemini_thinking = endpoint.provider == "gemini" and _is_gemini_thinking_model(
            endpoint.model
        )
        needs_thinking_budget = is_gpt_oss or is_gemini_thinking
        min_budget = 1536 if needs_thinking_budget else max_tokens
        token_budget = max(max_tokens, min_budget)
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if endpoint.provider == "groq" and is_gpt_oss:
            payload["max_completion_tokens"] = token_budget
            payload["reasoning_effort"] = "low"
        else:
            payload["max_tokens"] = token_budget
        if is_gemini_thinking:
            # OpenAI-compat maps this to Gemini thinking_level (default is high/dynamic).
            # flash-latest (3.x Flash) rejects MINIMAL; LOW is the fast supported level.
            payload["reasoning_effort"] = "low"
        timeout = 120.0 if needs_thinking_budget else 60.0
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content")
        if isinstance(text, list):
            parts = []
            for block in text:
                if isinstance(block, dict) and "text" in block:
                    parts.append(str(block["text"]))
                else:
                    parts.append(str(block))
            text = "\n".join(parts)
        text = str(text or "").strip()
        if not text:
            finish = choice.get("finish_reason")
            raise RuntimeError(
                f"empty model response (finish_reason={finish!r}; "
                "reasoning models may need a higher token budget)"
            )
        return text

    def cloud_endpoints(self) -> list[LLMEndpoint]:
        """Cloud/local chat endpoints only (excludes the terminal template step)."""
        return [e for e in self.chain if e.provider != "template"]

    def try_complete(
        self,
        endpoint: LLMEndpoint,
        system: str,
        user: str,
        *,
        temperature: float = 0.55,
        max_tokens: int = 1800,
    ) -> str | None:
        """Invoke one endpoint with a single 429 retry. Returns None on failure."""
        for attempt in range(2):
            try:
                text = self._invoke_endpoint(
                    endpoint,
                    system,
                    user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if not text.strip():
                    raise RuntimeError("empty model response")
                self.last_endpoint = endpoint.label
                return text
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.last_error = f"{endpoint.label}: {err[:300]}"
                if ("429" in err or "RESOURCE_EXHAUSTED" in err) and attempt == 0:
                    m = re.search(r"[Rr]etry in ([\d.]+)", err)
                    wait = float(m.group(1)) if m else 2.0
                    wait = min(max(wait, 0.5), 8.0)
                    logger.warning(
                        "LLM rate-limited on %s — retry in %.1fs",
                        endpoint.label,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                logger.warning(
                    "LLM failed on %s (%s)",
                    endpoint.label,
                    err[:160],
                )
                return None
        return None

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.55,
        max_tokens: int = 1800,
    ) -> str:
        """
        Return model text, or ``TEMPLATE_SENTINEL`` when all cloud endpoints fail /
        the chain reaches the template step.
        """
        self.last_endpoint = None
        self.last_error = None
        self.last_used_template = False
        errors: list[str] = []

        for endpoint in self.cloud_endpoints():
            text = self.try_complete(
                endpoint,
                system,
                user,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if text:
                if errors:
                    logger.info("LLM ok via %s", endpoint.label)
                return text
            if self.last_error:
                errors.append(self.last_error[:180])

        self.last_used_template = True
        self.last_endpoint = "template"
        if errors:
            self.last_error = " | ".join(errors)[:500]
            logger.warning(
                "All LLM endpoints failed — using template. Last: %s",
                errors[-1][:200],
            )
        else:
            self.last_error = None
        return TEMPLATE_SENTINEL

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1800,
    ) -> dict[str, Any]:
        text = self.complete(
            system + "\nRespond with JSON only. No markdown fences.",
            user,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if text == TEMPLATE_SENTINEL:
            return {}
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
        if fence:
            text = fence.group(1)
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


def get_llm_chain(
    *,
    yaml_model: str | None = None,
    app_title: str = "llm-chain",
    app_referer: str = "https://github.com/llm-chain",
) -> LLMChain:
    return LLMChain(
        yaml_model=yaml_model,
        app_title=app_title,
        app_referer=app_referer,
    )
