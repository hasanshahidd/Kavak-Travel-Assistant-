"""Smoke tests for every shipped prompt.

Verifies each prompt loads with valid frontmatter, declares the variables
the agent will provide at runtime, has a non-trivial body, and a
content-hash worth tracking. This is the safety net that prevents a
typo in a prompt from breaking the agent at runtime.

Block 3 acceptance gate: every prompt template in ``app/prompts/`` loads
cleanly and the CHANGELOG records a v1 entry per prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.llm.prompt_loader import clear_cache, load_prompt

# ---------------------------------------------------------------------------
# Per-prompt expectations
# ---------------------------------------------------------------------------

# (prompt_name, expected_id, expected_required_variables)
PROMPT_SPECS: list[tuple[str, str, set[str]]] = [
    ("router", "router.v10", {"conversation_summary", "user_message"}),
    ("extractor", "extractor.v4", {"conversation_summary", "user_message"}),
    ("clarifier", "clarifier.v2", {"missing_fields", "user_message", "conversation_summary"}),
    ("rag_answer", "rag_answer.v3", {"user_question", "retrieved_chunks", "conversation_context"}),
    (
        "flight_responder",
        "flight_responder.v2",
        {"user_query", "relaxation_summary", "flight_results"},
    ),
    ("oos_reply", "oos_reply.v4", {"user_message", "flight_inventory", "kb_inventory"}),
]


@pytest.mark.parametrize(("name", "expected_id", "expected_vars"), PROMPT_SPECS)
def test_prompt_loads_and_declares_expected_variables(
    name: str, expected_id: str, expected_vars: set[str]
) -> None:
    clear_cache()
    tpl = load_prompt(name)
    assert tpl.frontmatter.id == expected_id
    assert tpl.required_variables() == expected_vars
    assert tpl.body_hash, "body_hash must be populated"
    assert len(tpl.body) > 200, f"prompt {name} body suspiciously short"


@pytest.mark.parametrize("name", [spec[0] for spec in PROMPT_SPECS])
def test_prompt_renders_with_dummy_variables(name: str) -> None:
    """Every prompt should render with placeholder values without leftover markers."""
    clear_cache()
    tpl = load_prompt(name)
    dummies = {var: f"<{var}>" for var in tpl.required_variables()}
    rendered = tpl.render(dummies)
    assert "{{" not in rendered, f"unrendered marker remained in {name}"
    assert "}}" not in rendered


def test_all_prompts_have_distinct_hashes() -> None:
    """Sanity: no two prompts share a body hash (would indicate accidental copy)."""
    clear_cache()
    hashes = {load_prompt(name).body_hash for name, _, _ in PROMPT_SPECS}
    assert len(hashes) == len(PROMPT_SPECS), "duplicate body_hash across prompts"


def test_extractor_prompt_pins_todays_date() -> None:
    """The extractor must anchor relative-date resolution at a specific date."""
    clear_cache()
    tpl = load_prompt("extractor")
    assert "2026-05-02" in tpl.body, "extractor must pin today's date for relative-date resolution"


def test_extractor_prompt_includes_negation_few_shot() -> None:
    """The extractor must show how to handle 'avoid overnight' as a negation, not a positive filter."""
    clear_cache()
    tpl = load_prompt("extractor")
    body = tpl.body.lower()
    assert "avoid overnight" in body
    assert "negation" in body, "extractor must explicitly call out negation handling"


def test_rag_answer_prompt_enforces_citations() -> None:
    """The RAG prompt must require citations and document the refusal path."""
    clear_cache()
    tpl = load_prompt("rag_answer")
    body = tpl.body.lower()
    assert "citation" in body
    assert "is_refusal" in body, "RAG prompt must document the explicit refusal path"
    assert "verbatim" in body, "RAG prompt must require verbatim spans for verifier compatibility"


def test_router_prompt_pins_two_geography_rule() -> None:
    """Router must disambiguate single-geography (OOS) vs two-geography (flight_search).

    v10 fix preventing UAE-UK / Dubai-Reykjavik from mis-routing to OOS.
    """
    clear_cache()
    tpl = load_prompt("router")
    body = tpl.body.lower()
    assert "two geographies" in body or "two cities" in body or "two named" in body, (
        "router must explicitly disambiguate single vs two-geography routing"
    )


def test_router_prompt_pins_origin_only_continuation() -> None:
    """Router must handle 'from <city>' as a flight refinement, not OOS.

    v9 fix for the multi-turn t6 'from Mumbai' case.
    """
    clear_cache()
    tpl = load_prompt("router")
    body = tpl.body.lower()
    assert "origin-only" in body, "router must document origin-only follow-ups"
    assert "from mumbai" in body, "router must show the worked example"


def test_clarifier_prompt_enforces_one_question_rule() -> None:
    """The clarifier must explicitly state the one-question constraint."""
    clear_cache()
    tpl = load_prompt("clarifier")
    body = tpl.body.lower()
    assert "one question" in body or "one clarifying question" in body


def test_changelog_documents_every_shipped_prompt() -> None:
    """Every prompt id in the spec list must appear in CHANGELOG.md."""
    settings = get_settings()
    changelog_path: Path = settings.prompts_dir / "CHANGELOG.md"
    assert changelog_path.exists()
    body = changelog_path.read_text(encoding="utf-8")
    for _name, prompt_id, _vars in PROMPT_SPECS:
        assert prompt_id in body, f"CHANGELOG missing entry for {prompt_id}"


def test_shared_reference_docs_exist() -> None:
    """Persona + safety reference docs ship even though they're not loadable prompts."""
    settings = get_settings()
    for fname in ("persona.md", "safety.md"):
        path = settings.prompts_dir / "_shared" / fname
        assert path.exists(), f"shared reference doc missing: {fname}"
        assert path.stat().st_size > 300, f"shared doc {fname} suspiciously short"
