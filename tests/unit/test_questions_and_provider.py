"""Question understanding and the optional model provider (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from repolens.llm.provider import (
    AnthropicProvider,
    ProviderUnavailable,
    prepare_evidence,
    validate_answer,
)
from repolens.services.questions import Intent, extract_terms

EVIDENCE = [
    {"id": "E1", "category": "SOURCE_FACT", "statement": "f is defined", "text": "def f(): ..."},
    {"id": "E2", "category": "STATIC_RELATIONSHIP", "statement": "g calls f", "text": None},
]


@pytest.mark.parametrize(
    ("question", "intent", "identifiers"),
    [
        ("Where is with_tax implemented?", Intent.DEFINITION, ["with_tax"]),
        ("Who calls inventory.pricing.quote?", Intent.CALLERS, ["inventory.pricing.quote"]),
        ("What could break if I change Item.total?", Intent.IMPACT, ["Item.total"]),
        ("Which tests cover with_tax?", Intent.TESTS, ["with_tax"]),
        ("Which relationships are unresolved in load_plugin?", Intent.UNRESOLVED, ["load_plugin"]),
        ("Tell me about ShoppingCart", Intent.GENERAL, ["ShoppingCart"]),
    ],
)
def test_extract_terms(question: str, intent: Intent, identifiers: list[str]) -> None:
    terms = extract_terms(question)
    assert terms.intent is intent
    assert terms.identifiers == identifiers


def test_extract_terms_paths_and_keywords() -> None:
    terms = extract_terms("Where is authentication handled in app/auth.py?")
    assert terms.paths == ["app/auth.py"]
    assert "authentication" in terms.keywords and "handled" in terms.keywords
    assert "where" not in [k.lower() for k in terms.keywords]


def test_source_is_withheld_unless_allowed() -> None:
    withheld = prepare_evidence(EVIDENCE, allow_source=False, max_chars=10_000)
    assert all("text" not in item for item in withheld)
    allowed = prepare_evidence(EVIDENCE, allow_source=True, max_chars=10_000)
    assert allowed[0]["text"] == "def f(): ..."
    budgeted = prepare_evidence(EVIDENCE, allow_source=True, max_chars=120)
    assert len(budgeted) == 1  # whole items only, within the budget


def test_validate_answer_rejects_invented_citations() -> None:
    ok = validate_answer(
        {
            "explanation": "g calls f [E2] and f is defined [E1].",
            "cited_evidence": ["E1"],
            "evidence_sufficient": True,
        },
        {"E1", "E2"},
        "m",
    )
    assert ok.cited_ids == ["E1", "E2"]
    with pytest.raises(ProviderUnavailable, match="unknown evidence"):
        validate_answer({"explanation": "x [E9]", "cited_evidence": []}, {"E1"}, "m")
    with pytest.raises(ProviderUnavailable):
        validate_answer({"explanation": "  "}, {"E1"}, "m")


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def provider(messages: FakeMessages, *, allow_source: bool = False) -> AnthropicProvider:
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return AnthropicProvider(
        model="claude-opus-5-5",
        effort="medium",
        timeout_seconds=5,
        allow_source=allow_source,
        max_evidence_chars=10_000,
        min_interval_seconds=0,
        client=client,
    )


def response(text: str, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        model="claude-opus-5-5",
    )


def test_provider_request_shape_and_success() -> None:
    body = {"explanation": "g calls f [E2].", "cited_evidence": ["E2"], "evidence_sufficient": True}
    messages = FakeMessages(response(json.dumps(body)))
    answer = provider(messages).explain("who calls f?", EVIDENCE)
    assert answer.cited_ids == ["E2"] and answer.model == "claude-opus-5-5"
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    sent = json.loads(call["messages"][0]["content"])
    assert all("text" not in item for item in sent["evidence"])  # source withheld by default
    assert "data" in call["system"] and "ignored" in call["system"]


@pytest.mark.parametrize(
    "messages",
    [
        FakeMessages(response("{}", stop_reason="refusal")),
        FakeMessages(response("not json")),
        FakeMessages(response(json.dumps({"explanation": "x [E7]", "cited_evidence": []}))),
        FakeMessages(error=TimeoutError("slow")),
    ],
)
def test_provider_failures_degrade(messages: FakeMessages) -> None:
    with pytest.raises(ProviderUnavailable):
        provider(messages).explain("q", EVIDENCE)


def test_missing_sdk_or_evidence_is_unavailable() -> None:
    with pytest.raises(ProviderUnavailable):
        provider(FakeMessages()).explain("q", [])


def test_provider_refuses_instead_of_waiting_when_rate_limited() -> None:
    body = {"explanation": "g calls f [E2].", "cited_evidence": ["E2"], "evidence_sufficient": True}
    messages = FakeMessages(response(json.dumps(body)))
    p = provider(messages)
    p.min_interval = 60.0
    evidence = [{"id": "E2", "category": "STATIC_RELATIONSHIP", "statement": "g calls f"}]
    p.explain("who calls f?", evidence)
    with pytest.raises(ProviderUnavailable, match="rate limit"):
        p.explain("who calls f?", evidence)
    assert len(messages.calls) == 1
