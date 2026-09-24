"""Contracts for optional server-side browser evidence explanations."""

from __future__ import annotations

from cisco_vmanage_mcp.services.browser_ai import (
    build_browser_explainer,
    evidence_prompt,
)


def test_evidence_prompt_is_bounded_and_redacts_credentials() -> None:
    prompt = evidence_prompt(
        "Why is site 100 degraded?",
        {
            "kind": "health",
            "answer": "password=never-show-this",
            "data": {"api_key": "also-secret", "health": "critical"},
        },
        max_characters=500,
    )

    assert len(prompt) <= 500
    assert "never-show-this" not in prompt
    assert "also-secret" not in prompt
    assert "critical" in prompt


def test_browser_explainer_is_disabled_without_explicit_provider(monkeypatch) -> None:
    monkeypatch.delenv("BROWSER_AI_PROVIDER", raising=False)

    assert build_browser_explainer() is None
