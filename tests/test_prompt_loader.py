"""Prompt loader tests.

Cover the contract that the rest of the agent depends on:

* Frontmatter is parsed and validated strictly.
* Body hashing is deterministic and excludes frontmatter.
* Variable rendering is explicit - missing vars raise instead of silently
  producing a malformed prompt the LLM has to deal with.
* Real on-disk prompts (router.md) load cleanly.
"""

from __future__ import annotations

import textwrap

import pytest

from app.llm.prompt_loader import (
    PromptLoadError,
    clear_cache,
    load_prompt,
    load_prompt_from_text,
    parse_prompt_text,
)

# ---------------------------------------------------------------------------
# Frontmatter validation
# ---------------------------------------------------------------------------


def _make(meta: str, body: str = "Hello {{name}}") -> str:
    return f"---\n{meta}\n---\n\n{body}\n"


def test_loads_valid_prompt_from_text() -> None:
    text = _make(
        textwrap.dedent("""
        id: extractor.v1
        purpose: Convert NL to FlightQuery JSON.
        model: gpt-4o-mini
        temperature: 0
    """).strip()
    )
    tpl = load_prompt_from_text(text)
    assert tpl.frontmatter.id == "extractor.v1"
    assert tpl.frontmatter.temperature == 0
    assert tpl.frontmatter.model == "gpt-4o-mini"
    assert tpl.body.startswith("Hello")


def test_missing_frontmatter_rejected() -> None:
    with pytest.raises(PromptLoadError):
        load_prompt_from_text("Just a body, no frontmatter.")


def test_extra_frontmatter_keys_rejected() -> None:
    text = _make(
        textwrap.dedent("""
        id: extractor.v1
        purpose: Convert NL to FlightQuery JSON.
        model: gpt-4o-mini
        temperature: 0
        unknown_key: oops
    """).strip()
    )
    with pytest.raises(PromptLoadError):
        load_prompt_from_text(text)


def test_invalid_id_format_rejected() -> None:
    text = _make(
        textwrap.dedent("""
        id: ExtractorV1
        purpose: Convert NL to FlightQuery JSON.
        model: gpt-4o-mini
        temperature: 0
    """).strip()
    )
    with pytest.raises(PromptLoadError):
        load_prompt_from_text(text)


def test_temperature_out_of_range_rejected() -> None:
    text = _make(
        textwrap.dedent("""
        id: foo.v1
        purpose: An impossibly hot prompt.
        model: gpt-4o-mini
        temperature: 5.0
    """).strip()
    )
    with pytest.raises(PromptLoadError):
        load_prompt_from_text(text)


def test_empty_body_rejected() -> None:
    text = "---\nid: foo.v1\npurpose: missing body content\nmodel: gpt-4o-mini\ntemperature: 0\n---\n\n"
    with pytest.raises(PromptLoadError):
        parse_prompt_text(text)


# ---------------------------------------------------------------------------
# Hashing - same body, same hash; tweaked frontmatter doesn't change it
# ---------------------------------------------------------------------------


def test_body_hash_is_deterministic() -> None:
    text = _make(
        textwrap.dedent("""
        id: foo.v1
        purpose: hash test
        model: gpt-4o-mini
        temperature: 0
    """).strip(),
        body="Stable body content",
    )
    a = load_prompt_from_text(text)
    b = load_prompt_from_text(text)
    assert a.body_hash == b.body_hash
    assert len(a.body_hash) == 12


def test_body_hash_ignores_frontmatter_changes() -> None:
    body = "Identical body across both prompts."
    text_v1 = _make(
        textwrap.dedent("""
        id: foo.v1
        purpose: first version
        model: gpt-4o-mini
        temperature: 0
    """).strip(),
        body=body,
    )
    text_v2 = _make(
        textwrap.dedent("""
        id: foo.v2
        purpose: second version
        model: gpt-4o
        temperature: 0.3
        notes: |
          Bumped model + temperature for warmer tone.
    """).strip(),
        body=body,
    )
    assert load_prompt_from_text(text_v1).body_hash == load_prompt_from_text(text_v2).body_hash


def test_body_hash_changes_when_body_changes() -> None:
    head = textwrap.dedent("""
        id: foo.v1
        purpose: hash test
        model: gpt-4o-mini
        temperature: 0
    """).strip()
    a = load_prompt_from_text(_make(head, body="Original"))
    b = load_prompt_from_text(_make(head, body="Modified"))
    assert a.body_hash != b.body_hash


# ---------------------------------------------------------------------------
# Variable rendering
# ---------------------------------------------------------------------------


def test_render_substitutes_variables() -> None:
    text = _make(
        textwrap.dedent("""
        id: greet.v1
        purpose: trivial greeting
        model: gpt-4o-mini
        temperature: 0
    """).strip(),
        body="Hi {{name}}, your trip to {{city}} is booked.",
    )
    tpl = load_prompt_from_text(text)
    out = tpl.render({"name": "Hassan", "city": "Tokyo"})
    assert out == "Hi Hassan, your trip to Tokyo is booked."


def test_render_missing_variable_raises() -> None:
    text = _make(
        textwrap.dedent("""
        id: greet.v1
        purpose: trivial greeting
        model: gpt-4o-mini
        temperature: 0
    """).strip(),
        body="Hi {{name}}",
    )
    tpl = load_prompt_from_text(text)
    with pytest.raises(KeyError):
        tpl.render({})


def test_required_variables_set() -> None:
    text = _make(
        textwrap.dedent("""
        id: greet.v1
        purpose: trivial greeting
        model: gpt-4o-mini
        temperature: 0
    """).strip(),
        body="{{a}} and {{b}} and {{a}} again",
    )
    tpl = load_prompt_from_text(text)
    assert tpl.required_variables() == {"a", "b"}


# ---------------------------------------------------------------------------
# Real on-disk prompt loads (smoke test)
# ---------------------------------------------------------------------------


def test_router_prompt_loads_from_disk() -> None:
    """Real on-disk prompt loads. Version checked elsewhere; here we just confirm the loader handles a real file."""
    clear_cache()
    tpl = load_prompt("router")
    assert tpl.frontmatter.id.startswith("router.")
    assert tpl.frontmatter.temperature == 0
    assert tpl.required_variables() == {"conversation_summary", "user_message"}
    assert tpl.body_hash  # non-empty
