"""Tests for retriever query augmentation (v11).

Three augmentations matter for retrieval quality:

1. **City→country expansion** — "Tokyo" → "Tokyo (Japan)" so KB chunks
   titled by country still match user phrasings by city.
2. **Topic-keyword prefix** — "what items are restricted?" embeds
   closer to the baggage-policy doc than the refund-policy doc by
   prepending "baggage:" to the query.
3. **Conversation summary stitching** — short follow-ups inherit
   topical signal from the prior turn.

These tests pin the building blocks; the end-to-end behaviour is
covered by the live UAT.
"""

from __future__ import annotations

from app.graph.nodes.retriever import (
    _build_query,
    _detect_topic,
    _expand_city_to_country,
)

# ---------------------------------------------------------------------------
# Topic detection — bridges keyword → KB-doc gap
# ---------------------------------------------------------------------------


def test_detect_topic_baggage_keywords() -> None:
    """Baggage-related keywords map to the baggage doc."""
    assert _detect_topic("what items are restricted?") == "baggage"
    assert _detect_topic("can I bring sports equipment") == "baggage"
    assert _detect_topic("cabin baggage allowance") == "baggage"
    assert _detect_topic("checked bag weight limit") == "baggage"
    assert _detect_topic("my luggage was lost") == "baggage"


def test_detect_topic_refund_keywords() -> None:
    assert _detect_topic("can I cancel my ticket") == "refund"
    assert _detect_topic("refund processing time") == "refund"
    assert _detect_topic("change my booking date") == "refund"


def test_detect_topic_visa_keywords() -> None:
    assert _detect_topic("do I need a visa for Japan") == "visa"
    assert _detect_topic("ETIAS for UK passport") == "visa"


def test_detect_topic_no_match_returns_none() -> None:
    assert _detect_topic("flights to Tokyo") is None
    assert _detect_topic("hello there") is None


# ---------------------------------------------------------------------------
# City → country expansion
# ---------------------------------------------------------------------------


def test_expand_city_to_country_known_city() -> None:
    out = _expand_city_to_country("visa for Tokyo")
    assert "Japan" in out
    assert out.startswith("visa for Tokyo")


def test_expand_city_to_country_unknown_word_unchanged() -> None:
    assert _expand_city_to_country("hello world") == "hello world"


def test_expand_city_to_country_idempotent_on_country() -> None:
    """If the user already wrote the country, no double-annotation."""
    text = "visa for Japan"
    # No expansion because "Japan" isn't a city in the alias map
    assert _expand_city_to_country(text) == text


# ---------------------------------------------------------------------------
# Combined query builder — all augmentations layered
# ---------------------------------------------------------------------------


def test_build_query_layers_topic_and_city_expansion() -> None:
    """The classic case: 'visa for Tokyo' → 'visa: visa for Tokyo (Japan)'."""
    out = _build_query("visa for Tokyo", None)
    assert "visa:" in out  # topic prefix
    assert "Japan" in out  # city expansion


def test_build_query_includes_summary_when_present() -> None:
    summary = "Recent turns:\n  user: what visas do you cover\n  assistant: ..."
    out = _build_query("ok then tell me on Tokyo", summary)
    assert summary in out
    assert "Japan" in out  # Tokyo expansion still happens


def test_build_query_skips_placeholder_summary() -> None:
    out = _build_query("hello", "(no prior conversation)")
    # Placeholder summary should not be stitched in
    assert "(no prior conversation)" not in out


def test_build_query_no_summary_no_topic_returns_expanded_only() -> None:
    """Plain message with no topic / no summary → city expansion only."""
    out = _build_query("Tokyo", None)
    assert out == "Tokyo (Japan)"


def test_build_query_skips_summary_for_self_contained_query() -> None:
    """Any query with its OWN topic keyword skips summary stitching.

    Live UAT bug: 'Pakistani passport visa for UK' came after a Saudi-Schengen
    turn. Stitching the Saudi summary into the embedding biased retrieval
    toward Pakistani-Schengen. Self-contained queries (with own topic
    keyword) skip the summary stitching entirely.
    """
    biasing_summary = (
        "Recent turns:\n"
        "  user: Saudi passport visa for Schengen\n"
        "  assistant: Saudi passport holders can enter the Schengen Area..."
    )
    # 6 words, contains 'visa' (topic) → self-contained, summary skipped
    out = _build_query("Pakistani passport visa for UK", biasing_summary)
    assert "Saudi" not in out
    assert "Schengen" not in out
    assert out.startswith("visa: Pakistani passport visa for UK")


def test_build_query_skips_summary_for_short_topic_query() -> None:
    """Even short queries (< 5 words) skip summary if they carry a topic.

    Live UAT bug: *"sports equipment baggage"* (3 words, baggage topic)
    after a refund-policy turn pulled refund chunks because the
    refund-flavoured summary got prepended to the embedding. The fix
    is: topical signal (baggage / refund / visa) beats conversational
    signal regardless of query length.
    """
    refund_summary = (
        "Recent turns:\n"
        "  user: what's your refund policy\n"
        "  assistant: Refundable tickets can be cancelled up to 48h..."
    )
    out = _build_query("sports equipment baggage", refund_summary)
    assert "refund" not in out.lower()
    assert "Recent turns" not in out
    assert out.startswith("baggage: sports equipment baggage")


def test_build_query_keeps_summary_for_short_topicless_followup() -> None:
    """Short follow-ups WITHOUT own topic keyword STILL inherit summary.

    This is the original feature: 'tell me on Tokyo' after a visa-scope
    turn should pull in the visa context. 'Tokyo' alone has no topic
    keyword, so the conversational signal is what tells us this is a
    visa question.
    """
    summary = (
        "Recent turns:\n"
        "  user: what visas do you cover\n"
        "  assistant: I cover UAE, Indian, UK passports..."
    )
    # 4 words, no own topic keyword → still stitches summary
    out = _build_query("tell me on Tokyo", summary)
    assert "Recent turns" in out  # summary present
