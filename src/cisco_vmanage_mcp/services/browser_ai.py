"""Stateless, server-side AI explanations over normalized browser evidence."""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Protocol, cast

from cisco_vmanage_mcp.ai_providers import (
    PROVIDERS,
    get_saved_api_key,
    provider_ca_bundle_path,
)
from cisco_vmanage_mcp.services.investigations import sanitize_investigation_data

logger = logging.getLogger("cisco_vmanage_mcp.browser_ai")

DEFAULT_MAX_EVIDENCE_CHARACTERS = 30_000
MAX_EXPLANATION_CHARACTERS = 10_000

SYSTEM_PROMPT = """You are a Cisco SD-WAN operations evidence explainer.
Use only the supplied normalized evidence. Preserve uncertainty and partial-source warnings.
Do not invent device state, root cause, remediation, or completed actions.
Do not request or expose credentials. Keep the response concise and operational."""


class BrowserExplainer(Protocol):
    """Optional stateless provider used after deterministic evidence collection."""

    provider_name: str

    def explain(self, question: str, result: dict[str, Any]) -> str: ...


def evidence_prompt(
    question: str,
    result: dict[str, Any],
    *,
    max_characters: int = DEFAULT_MAX_EVIDENCE_CHARACTERS,
) -> str:
    """Build a redacted, bounded provider prompt from normalized evidence."""
    if max_characters < 200:
        raise ValueError("max_characters must be at least 200")
    safe_question = str(sanitize_investigation_data(question))[:2_000]
    safe_result = sanitize_investigation_data(result)
    prefix = f"Operator question:\n{safe_question}\n\nNormalized evidence:\n"
    evidence = json.dumps(safe_result, separators=(",", ":"), sort_keys=True, default=str)
    available = max(0, max_characters - len(prefix))
    return (prefix + evidence[:available])[:max_characters]


class _ClaudeExplainer:
    provider_name = "claude"

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic
        import httpx

        ca_bundle = provider_ca_bundle_path()
        self._http_client = httpx.Client(verify=ca_bundle or True)
        self._client: Any = anthropic.Anthropic(
            api_key=api_key,
            http_client=self._http_client,
        )
        self._model = model
        self._lock = threading.Lock()

    def explain(self, question: str, result: dict[str, Any]) -> str:
        with self._lock:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2_048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": evidence_prompt(question, result)}],
            )
        text = "\n".join(
            block.text for block in response.content if getattr(block, "text", None)
        )
        return text[:MAX_EXPLANATION_CHARACTERS] or "(No provider explanation)"

    def close(self) -> None:
        self._http_client.close()


class _OpenAIExplainer:
    provider_name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        import httpx
        from openai import OpenAI

        ca_bundle = provider_ca_bundle_path()
        self._http_client = httpx.Client(verify=ca_bundle or True)
        self._client: Any = OpenAI(api_key=api_key, http_client=self._http_client)
        self._model = model
        self._lock = threading.Lock()

    def explain(self, question: str, result: dict[str, Any]) -> str:
        with self._lock:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": evidence_prompt(question, result)},
                ],
            )
        text = response.choices[0].message.content or "(No provider explanation)"
        return str(text)[:MAX_EXPLANATION_CHARACTERS]

    def close(self) -> None:
        self._http_client.close()


class _GeminiExplainer:
    provider_name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        ca_bundle = provider_ca_bundle_path()
        if ca_bundle:
            os.environ.setdefault("SSL_CERT_FILE", ca_bundle)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_bundle)
        from google import genai
        from google.genai import types

        self._client: Any = genai.Client(api_key=api_key)
        self._types = types
        self._model = model
        self._lock = threading.Lock()

    def explain(self, question: str, result: dict[str, Any]) -> str:
        with self._lock:
            response = self._client.models.generate_content(
                model=self._model,
                contents=evidence_prompt(question, result),
                config=self._types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
            )
        return str(response.text or "(No provider explanation)")[:MAX_EXPLANATION_CHARACTERS]

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def build_browser_explainer() -> BrowserExplainer | None:
    """Build an explicitly selected provider when its server-side key is available."""
    provider_name = os.getenv("BROWSER_AI_PROVIDER", "").strip().lower()
    if not provider_name:
        return None
    if provider_name not in {"claude", "openai", "gemini"}:
        logger.warning("Unsupported BROWSER_AI_PROVIDER: %s", provider_name)
        return None
    provider = PROVIDERS[provider_name]
    api_key = os.getenv(cast(str, provider["env_key"]), "").strip()
    if not api_key:
        api_key = get_saved_api_key(provider_name) or ""
    if not api_key:
        logger.warning("Browser AI provider %s has no server-side key", provider_name)
        return None
    model = os.getenv("BROWSER_AI_MODEL", "").strip()
    default_models = {
        "claude": "claude-sonnet-4-20250514",
        "openai": "gpt-4o",
        "gemini": "gemini-2.5-flash",
    }
    try:
        if provider_name == "claude":
            return _ClaudeExplainer(api_key, model or default_models[provider_name])
        if provider_name == "openai":
            return _OpenAIExplainer(api_key, model or default_models[provider_name])
        return _GeminiExplainer(api_key, model or default_models[provider_name])
    except Exception as exc:
        logger.warning(
            "Browser AI provider initialization failed: %s",
            type(exc).__name__,
        )
        return None
