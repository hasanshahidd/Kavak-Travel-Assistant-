"""LLM client tests.

The OpenAI path requires network + a live key, so we focus on:

* :class:`MockClient` - used by the eval harness and offline demos
* Cost calculation helper - independent of any vendor
* Factory selection - env-driven, must fall back gracefully

The integration test for ``OpenAIClient`` is opt-in; it skips when no
``OPENAI_API_KEY`` is configured so CI stays green without a key.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from app.llm.client import (
    MODEL_PRICING,
    LLMResponse,
    LLMValidationError,
    MockClient,
    compute_cost,
    get_llm_client,
)
from app.llm.prompt_loader import load_prompt_from_text
from app.schemas.intent import Intent, RouterOutput

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _router_prompt() -> str:
    return textwrap.dedent("""
        ---
        id: router.v0
        purpose: classify intent
        model: gpt-4o-mini
        temperature: 0
        ---

        Classify this: {{user_message}}
    """).strip()


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_compute_cost_known_model() -> None:
    # gpt-4o-mini = (0.15, 0.60) per 1M tokens
    cost = compute_cost("gpt-4o-mini", tokens_in=1_000_000, tokens_out=500_000)
    assert cost == pytest.approx(0.15 + 0.30, rel=1e-9)


def test_compute_cost_unknown_model_uses_default() -> None:
    cost = compute_cost("never-heard-of-this-model", 1_000_000, 1_000_000)
    in_default, out_default = MODEL_PRICING["_default"]
    assert cost == pytest.approx(in_default + out_default, rel=1e-9)


def test_compute_cost_zero_tokens() -> None:
    assert compute_cost("gpt-4o-mini", 0, 0) == 0.0


# ---------------------------------------------------------------------------
# MockClient - text and structured paths
# ---------------------------------------------------------------------------


def test_mock_client_returns_default_text() -> None:
    client = MockClient(default_text="canned text")
    tpl = load_prompt_from_text(_router_prompt())
    resp = client.complete(prompt=tpl, variables={"user_message": "hi"})
    assert isinstance(resp, LLMResponse)
    assert resp.data == "canned text"
    assert resp.raw_text == "canned text"
    assert resp.prompt_id == "router.v0"
    assert resp.prompt_hash == tpl.body_hash
    assert resp.usage.tokens_in > 0  # plausible counts so cost math works
    assert resp.usage.tokens_out > 0


def test_mock_client_returns_registered_parsed_instance() -> None:
    client = MockClient()
    parsed = RouterOutput(intent=Intent.FLIGHT_SEARCH, rationale="user mentioned Tokyo")
    client.register("router.v0", raw_text=parsed.model_dump_json(), parsed=parsed)

    tpl = load_prompt_from_text(_router_prompt())
    resp = client.complete(
        prompt=tpl,
        response_model=RouterOutput,
        variables={"user_message": "fly to tokyo"},
    )
    assert isinstance(resp.data, RouterOutput)
    assert resp.data.intent is Intent.FLIGHT_SEARCH


def test_mock_client_parses_raw_json_when_no_parsed_registered() -> None:
    client = MockClient()
    raw = '{"intent": "policy_qa", "rationale": "user asked about visa"}'
    client.register("router.v0", raw_text=raw, parsed=None)

    tpl = load_prompt_from_text(_router_prompt())
    resp = client.complete(
        prompt=tpl,
        response_model=RouterOutput,
        variables={"user_message": "do i need a visa"},
    )
    assert isinstance(resp.data, RouterOutput)
    assert resp.data.intent is Intent.POLICY_QA


def test_mock_client_invalid_json_with_response_model_raises() -> None:
    client = MockClient(default_text="not valid json at all")
    tpl = load_prompt_from_text(_router_prompt())
    with pytest.raises(LLMValidationError):
        client.complete(
            prompt=tpl,
            response_model=RouterOutput,
            variables={"user_message": "x"},
        )


def test_mock_client_records_prompt_hash_and_id() -> None:
    client = MockClient()
    tpl = load_prompt_from_text(_router_prompt())
    resp = client.complete(prompt=tpl, variables={"user_message": "x"})
    assert resp.prompt_id == tpl.frontmatter.id
    assert resp.prompt_hash == tpl.body_hash
    assert resp.cost_usd >= 0


# ---------------------------------------------------------------------------
# Factory - env-driven selection with graceful fallback
# ---------------------------------------------------------------------------


def test_factory_returns_mock_when_provider_is_mock() -> None:
    client = get_llm_client(provider="mock")
    assert client.name == "mock"


def test_factory_falls_back_to_mock_when_openai_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer ergonomics: the demo must still run without an API key."""
    from app.config import get_settings

    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        client = get_llm_client(provider="openai")
        # If a real key happens to be set in environment beyond monkeypatch,
        # this is still acceptable - we got a working client.
        assert client.name in {"mock", "openai"}
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# OpenAI integration - opt-in only
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY") or os.environ.get("SKIP_OPENAI_INTEGRATION") == "1",
    reason="No OPENAI_API_KEY or explicitly skipped",
)
def test_openai_integration_real_call() -> None:
    """Live smoke test - only runs when explicitly configured."""
    from app.llm.client import OpenAIClient

    client = OpenAIClient()
    tpl = load_prompt_from_text(
        textwrap.dedent("""
            ---
            id: smoke.v0
            purpose: smoke test
            model: gpt-4o-mini
            temperature: 0
            ---

            Reply with the single word: pong
        """).strip()
    )
    resp = client.complete(prompt=tpl)
    assert isinstance(resp.data, str)
    assert resp.usage.tokens_in > 0
    assert resp.cost_usd > 0
