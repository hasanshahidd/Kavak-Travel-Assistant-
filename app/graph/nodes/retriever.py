"""Retriever graph node — wraps :class:`KBRetriever` with tracing.

Pure function ``retrieve()`` that Block 6 lifts into the LangGraph state
machine. Designed to be testable in isolation by passing a retriever and a
tracer; no implicit dependencies on graph state shape.

Multi-turn aware: when a ``conversation_summary`` is provided, the
embedding query is built as ``"<summary topic>\n\n<user_message>"``.
This is the cheap fix for the classic RAG failure — *"tell me on tokyo"*
after the user already asked about visa coverage. With just the user
message, the embedding has no policy keyword and the threshold gate
returns nothing. With the summary stitched in, the embedding picks up
"visa" / "refund" / "baggage" from the prior turn and retrieves the
right section. No extra LLM call; one string concat.
"""

from __future__ import annotations

import re
import time

from app.llm.tracing import Tracer
from app.schemas.rag import Chunk
from app.tools.kb_retriever import KBRetriever
from app.utils.airports import country_for_city

# Default thresholds — also referenced by the answerer's refusal path.
#
# DEFAULT_MIN_SCORE was 0.5 originally; lowered to 0.4 after measuring real
# OpenAI text-embedding-3-small scores against this KB. The empirical
# distribution looks like:
#   * Direct in-vocabulary hit ("Do UAE passport holders need a visa for
#     Japan?")        → top chunk 0.55+
#   * Indirect / paraphrased ("ok then tell me on Tokyo" with visa context,
#     city→country expanded)                                  → top chunk 0.41-0.45
#   * Unrelated / wrong section                                → 0.20-0.34
# The natural break point is ~0.4. The verifier-stripped citation contract
# (answerer.py) already protects against false-positive matches even when
# the threshold is generous, so this trades a tighter recall ceiling for
# a slightly looser precision floor — exactly the right direction.
DEFAULT_TOP_K = 4
DEFAULT_MIN_SCORE = 0.4


# Match runs of letters (Unicode-aware) so we can spot city words in the
# user's message and check them against the airport alias map.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]+")


# Topic-keyword routing for retrieval. When a user's question implies a
# specific KB topic (baggage / refund / visa) but doesn't use the topic
# word itself, we prepend the topic so the embedding lands in the right
# section. Without this, *"what items are restricted?"* embeds closer to
# refund-policy chunks than baggage-policy chunks because both contain
# constraint language.
_TOPIC_KEYWORDS: dict[str, str] = {
    # Baggage signals
    "baggage": "baggage",
    "bag": "baggage",
    "luggage": "baggage",
    "suitcase": "baggage",
    "carry-on": "baggage",
    "carry on": "baggage",
    "cabin": "baggage",
    "checked": "baggage",
    "sports": "baggage",
    "equipment": "baggage",
    "restricted": "baggage",
    "lost": "baggage",
    "delayed bag": "baggage",
    "weight limit": "baggage",
    "kg": "baggage",
    "allowance": "baggage",
    # Refund signals
    "refund": "refund",
    "cancel": "refund",
    "cancellation": "refund",
    "refundable": "refund",
    "non-refundable": "refund",
    "change": "refund",
    "rebook": "refund",
    "reschedule": "refund",
    # Visa signals (already strong but reinforce for short queries)
    "visa": "visa",
    "passport": "visa",
    "etias": "visa",
    "esta": "visa",
    "eta": "visa",
}


def _detect_topic(text: str) -> str | None:
    """Return the dominant KB topic keyword in ``text``, if any."""
    lo = text.lower()
    for needle, topic in _TOPIC_KEYWORDS.items():
        if needle in lo:
            return topic
    return None


def _expand_city_to_country(text: str) -> str:
    """Append "(Country)" hints for any city mentioned in ``text``.

    The KB sections are titled by country (*UAE passport — Japan*), but
    real users phrase visa questions by city (*"visa for Tokyo"*). Cosine
    similarity between *Tokyo* and *Japan* is positive but often under
    the 0.5 relevance gate. Adding the country as a hint lifts the score
    above the threshold reliably without touching the threshold itself.

    We only add hints for words we recognise as cities — unknown words
    are left alone, so this can't introduce noise.
    """
    seen: set[str] = set()
    hints: list[str] = []
    for match in _WORD_RE.finditer(text):
        word = match.group(0)
        country = country_for_city(word)
        if country and country.lower() != word.lower() and country not in seen:
            hints.append(country)
            seen.add(country)
    if not hints:
        return text
    return f"{text} ({' '.join(hints)})"


def _build_query(question: str, conversation_summary: str | None) -> str:
    """Combine the user message with conversation context for embedding.

    Three augmentations, each targeting a specific embedding-space gap:
      1. **City→country** (*Tokyo* → *Tokyo (Japan)*) so KB sections
         titled by country still match user phrasings by city.
      2. **Topic prefix** (*"sports equipment"* → *"baggage: sports
         equipment"*) so the embedding lands in the right KB doc instead
         of an adjacent one with similar constraint language.
      3. **Conversation summary stitching** so short follow-ups
         (*"tell me on Tokyo"* after a visa-scope turn) inherit the
         topical signal of the prior turn.

    **Important precision rule** (added after live UAT discovered the
    failure mode): rule 3 only applies when the new question is a
    *short follow-up* that lacks topical signal of its own. A long,
    self-contained question like *"Pakistani passport visa for UK"*
    already has all the signal the embedding needs; stitching the
    prior summary in (e.g. a previous Saudi-Schengen turn) actively
    *biases* the embedding toward the prior topic and pulls the wrong
    chunk to the top.
    """
    expanded = _expand_city_to_country(question)
    topic = _detect_topic(expanded)
    if topic:
        expanded = f"{topic}: {expanded}"
    if not conversation_summary or conversation_summary.strip().startswith("("):
        return expanded

    # Self-contained query: any query that carries its OWN topic keyword
    # (baggage / refund / visa). The topic prefix already steers the
    # embedding to the right KB section; stitching a prior-turn summary
    # in would only bias retrieval toward whatever the prior turn was
    # about.
    #
    # Live UAT bug that motivated the strict version of this rule:
    # *"sports equipment baggage"* (3 words, baggage topic) right after
    # a *"what's your refund policy"* turn pulled refund chunks because
    # the refund-flavoured summary got prepended to the embedding. The
    # earlier rule only skipped the summary when the query had 5+ words;
    # that gate let short-but-self-contained queries through. Now: if
    # the query has a detected topic, skip the summary regardless of
    # length. Topical signal beats conversational signal.
    if topic is not None:
        return expanded

    return f"{conversation_summary.strip()}\n\n{expanded}"


def retrieve(
    *,
    question: str,
    kb: KBRetriever,
    conversation_summary: str | None = None,
    tracer: Tracer | None = None,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[Chunk]:
    """Retrieve top-k chunks above the relevance threshold for a user question.

    ``tracer``, when provided, gets a ``"retriever"`` event with the
    embedded query, chunk ids, scores, and threshold — exactly what the
    Streamlit sidebar needs to render the agent's reasoning.
    """
    started = time.perf_counter()
    embed_query = _build_query(question, conversation_summary)
    chunks = kb.search(embed_query, top_k=top_k, min_score=min_score)
    elapsed_ms = (time.perf_counter() - started) * 1000

    if tracer is not None:
        tracer.emit(
            node="retriever",
            latency_ms=elapsed_ms,
            output={
                "question": question,
                "embed_query": embed_query,
                "used_summary": embed_query != question,
                "top_k": top_k,
                "min_score": min_score,
                "result_count": len(chunks),
                "below_threshold": len(chunks) == 0,
                "chunks": [
                    {"id": c.id, "doc": c.doc, "section": c.section, "score": c.score}
                    for c in chunks
                ],
            },
        )

    return chunks


__all__ = ["DEFAULT_MIN_SCORE", "DEFAULT_TOP_K", "retrieve"]
