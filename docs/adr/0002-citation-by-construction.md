# ADR 0002 - Citation-by-construction RAG

**Date:** 2026-05-02
**Status:** Accepted

## Context

The dominant RAG failure mode is **hallucinated citations**: the model
produces an authoritative-sounding answer with a reference that either
points to nothing real, paraphrases the source instead of quoting it,
or attributes a claim to the wrong document. This is dangerous in a
travel-policy assistant where wrong visa info has real consequences.

Three options for handling it:

1. **Trust the model.** Ask the prompt to cite, hope it does well.
2. **Heuristic checks.** Look for inline `[1]` markers; warn if absent.
3. **Structural enforcement.** Require citations in the schema, verify
   each cited span post-hoc against the source corpus, strip what can't
   be verified.

## Decision

Implement option 3 - **citation-by-construction**. The architecture has
three reinforcing layers:

1. **Schema-level enforcement** ([`app/schemas/rag.py`](../../app/schemas/rag.py)).
   `RagAnswer` requires `citations: list[Citation]`. Each `Citation` has
   a verbatim `span` (8-400 chars) tied to a `doc` filename. Empty
   citations are valid only when `is_refusal=True`.

2. **Retrieval threshold gate**
   ([`app/tools/kb_retriever.py`](../../app/tools/kb_retriever.py)). When
   no chunk scores above 0.5 cosine similarity, the retriever returns
   `[]` - forcing the answerer down its structural-refusal path
   *without* an LLM call.

3. **Post-hoc verifier** ([`app/llm/verifier.py`](../../app/llm/verifier.py)).
   For every cited span, check that it appears as a verbatim substring
   (whitespace-tolerant, case-insensitive on doc names) in some chunk
   from the cited doc. Strip every citation that fails. If all citations
   strip on a non-refusal answer, convert the answer to a refusal.

## Why this beats the alternatives

| Failure mode | Trust the model | Heuristic checks | Citation-by-construction |
|---|---|---|---|
| Model invents a citation | Ships | May ship | **Stripped** |
| Model paraphrases instead of quoting | Ships | May ship | **Stripped** |
| Model cites the wrong doc | Ships | May ship | **Stripped** |
| Model refuses unnecessarily | Cheap | Cheap | Cheap (threshold gate prevents) |
| Cost overhead | None | None | One substring search per citation |

The verifier is **deterministic, fast, and cheap** - no extra LLM call.
It's strictly stricter than what the model could enforce on itself.

## Why "verbatim" instead of "fuzzy match"

Fuzzy matching (Levenshtein, n-gram, embedding similarity) sounds
generous but is exactly the slippery-slope behaviour that lets
hallucinations through. A paraphrased citation that's "close enough"
shares the same epistemics as a fabricated one - both are claims the
model produced that don't actually exist in the source.

The cost of strict verbatim is some valid citations getting stripped
because the model lightly reworded. We accept that - a stripped citation
on a fact that's actually in the corpus is fine; the verifier converts
to refusal, the user retries with different phrasing or accepts the
"I don't have that info" answer. The cost of the *opposite* error
(shipping a hallucinated-but-plausible citation) is a wrong answer
treated as authoritative.

**Refusal beats fabrication.** The verifier encodes that.

## Alternatives considered

* **Inline citations as `[1]` markers in the prose** - popular but
  toothless. The marker is just text the model produced; nothing
  verifies it points to anything real.
* **Embedding similarity for citation matching** - adds an embedding call
  per citation, has fuzziness-induced false positives.
* **LLM-as-judge for citation accuracy** - burns another LLM call,
  introduces a second oracle that can also hallucinate.
* **Refuse on any retrieval miss** - too aggressive; lots of valid
  questions have moderate retrieval relevance that's still answerable.

## Consequences

**Positive:**
- Hallucination is structurally impossible at the answer surface
- Zero LLM cost for verification
- Refusal path is well-defined and tested
- Verifier returns a `VerificationReport` so traces explain *why* a
  citation was stripped (debugging affordance)

**Negative:**
- Strict verbatim matching may strip lightly-paraphrased valid citations
- Adds a small surface area (~150 lines) to maintain
- The model has to learn to quote-not-paraphrase via the prompt - see
  the [`rag_answer.md`](../../app/prompts/rag_answer.md) "must be a
  substring" rule that calls this out explicitly

**Operational note:** the verifier emits enough trace data
(`citations_stripped`, span previews, `converted_to_refusal`) that a
reviewer can debug a "why did this become a refusal" turn from disk in
under a minute via the trace replay tool.
