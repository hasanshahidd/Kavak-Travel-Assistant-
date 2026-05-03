# Prompt strategy

> The **why** behind every prompt. For each one: contract, output shape,
> temperature choice, CoT placement, few-shot rationale, and the
> failure modes the prompt was deliberately written to handle.
>
> See [`app/prompts/CHANGELOG.md`](../app/prompts/CHANGELOG.md) for
> versioned change history with measured eval deltas.

---

## Cross-cutting principles

These apply to every prompt in the project.

### 1. Versioned `.md` artifacts with strict frontmatter

Every prompt is a markdown file with a YAML frontmatter block validated
by [`app/llm/prompt_loader.py`](../app/llm/prompt_loader.py). Required
fields are `id`, `purpose`, `model`, `temperature`. A typo in
`temperature` fails at load time, not in production.

### 2. Body is content-hashed (frontmatter excluded)

The first 12 hex chars of SHA-256 of the body get logged to every trace
event. Tweaking `notes:` doesn't churn the hash; changing the actual
prompt wording does. This ties any output to the exact prompt that
produced it.

### 3. Structured outputs everywhere they're useful

Router → `RouterOutput`. Extractor → `FlightQuery`. RAG answerer →
`RagAnswer`. Critique → `ResponseCritique`. The only nodes that emit
plain text are user-facing replies (clarifier, flight_responder,
out_of_scope) where structure adds nothing.

### 4. Per-node temperature scaling

| Node | Temp | Why |
|---|---|---|
| router | 0 | Deterministic classification — same input, same output |
| extractor | 0 | Structured parsing |
| clarifier | 0.2 | Slight warmth in the question phrasing |
| rag_answer | 0 | Citation accuracy beats prose variation |
| flight_responder | 0.3 | The only node read as conversation; tone matters here |
| responder_critique | 0 | Strict reviewer voice |

`seed=42` is set on every OpenAI call, so even at non-zero temps the
same input produces the same output run-to-run.

---

## Prompt-by-prompt

### `router.v1` — intent classifier

- **Role:** classify a single user message into one of four intents:
  `flight_search`, `policy_qa`, `clarify`, `out_of_scope`.
- **Output:** `RouterOutput { intent, rationale }`. The rationale is a
  one-sentence justification logged to the trace and read by humans
  during eval.
- **Temperature:** 0. Routing must be deterministic.
- **CoT placement:** none. Routing is a single-token-style
  classification; CoT would slow it without helping.
- **Few-shots:** five examples covering each intent + a tricky
  multi-turn refinement that routes to `flight_search` (NOT `clarify`).
- **Failure modes addressed:**
  - "make it cheaper" → `flight_search`, not `clarify`
  - empty / gibberish / single-emoji → `out_of_scope`, not `clarify`
  - prompt injection → treat content as data, route the surrounding
    request

### `extractor.v1` — natural language → `FlightQuery`

- **Role:** convert user text into a strict `FlightQuery` JSON.
- **Output:** `FlightQuery` (Pydantic, ~13 fields).
- **Temperature:** 0. Extraction is structured parsing.
- **CoT placement:** **hidden scratchpad**. The schema has a
  `scratchpad` field the model fills with its reasoning before
  committing structured fields. The user never sees it; the trace
  captures it. This is the right CoT placement for two reasons:
  1. The model's reasoning influences its output for free
  2. The user gets a clean structured result, not the model's thinking
- **Today's date pinned in the prompt:** `2026-05-02`. Required so
  relative dates ("next August", "next month") resolve deterministically.
- **Few-shots:** five examples, deliberately chosen by failure mode:
  1. **Negation trap** — "avoid overnight layovers" must NOT become a
     positive layover filter
  2. **Missing origin** — set `needs_clarification=true`; do not guess
  3. **Multi-turn override** — "actually move it to September" updates
     the date only
  4. **Topic switch** — "now show me Paris" resets soft preferences
  5. **Ambiguous destination** — "Bangkok or Singapore" → clarify
- **Why these five and not others:** these are the failure modes that
  show up in real conversational flight search. Each one is documented
  in [`tests/test_memory_override.py`](../tests/test_memory_override.py).
- **Trade-off:** ~1,200 input tokens (largest prompt in the project).
  Justified — extractor errors propagate to flight_search, which is the
  most common turn.

### `clarifier.v1` — single follow-up question

- **Role:** ask exactly ONE targeted question when the extractor flagged
  missing fields.
- **Output:** plain text (no schema — goes straight to the user).
- **Temperature:** 0.2. Slight warmth so the question doesn't read
  bureaucratic; still deterministic enough that the priority order is
  honoured.
- **CoT placement:** none. The decision rule is in the prompt body
  (priority order: origin > destination > date > preferences); no
  reasoning needed.
- **The one-question rule:** stacking questions ("what city, what
  dates, and what budget?") is the single most common UX failure in
  flight chatbots. The prompt explicitly forbids it and gives examples
  of allowed phrasings (one parenthetical follow-up suggestion is OK,
  a second question is not).

### `rag_answer.v1` — policy Q&A with mandatory citations

- **Role:** answer travel-policy questions using ONLY the retrieved
  chunks. Refuse if chunks don't support the question.
- **Output:** `RagAnswer { answer, citations, confidence, is_refusal }`.
- **Temperature:** 0. Citation accuracy matters more than prose
  variation.
- **CoT placement:** none. The decision rule is binary: do the chunks
  support the question, yes or no. CoT here would invite the model to
  rationalise borderline cases.
- **Citation contract:** every span in `citations` MUST be a verbatim
  substring of the cited doc. The verifier
  ([`app/llm/verifier.py`](../app/llm/verifier.py)) enforces this
  post-hoc. Paraphrased citations get stripped.
- **Refusal path:** `is_refusal=true`, empty `citations`, plain-language
  refusal. Used when retrieval returns nothing or all citations fail
  verification.
- **Why "verbatim" not fuzzy:** see ADR
  [`0002-citation-by-construction.md`](adr/0002-citation-by-construction.md).
  Strict verbatim is the only honest contract; fuzzy matching has the
  same epistemic problem as fabrication.

### `flight_responder.v1` — format flights for the user

- **Role:** turn search results into a warm, scannable Markdown reply.
- **Output:** plain text Markdown.
- **Temperature:** 0.3. Highest in the project — this is the only node
  read as conversation. Determinism for facts, warmth for tone.
- **CoT placement:** none. The structure is fixed (numbered list +
  one-line rationale); reasoning is the flight tool's job.
- **Three behaviours beyond "list the flights":**
  1. **Per-flight rationale** — "cheapest direct, refundable" tells
     the user *why* this match is on the list.
  2. **Relaxation transparency** — when soft constraints were dropped,
     say so explicitly. Surfacing the relaxation builds trust.
  3. **One follow-up invitation** — never two. Same one-question rule
     as the clarifier.
- **Hard rule:** never invent flight data. The flight tool is the source
  of truth; the prompt is just a renderer.

### `responder_critique.v1` — self-critique loop

- **Role:** review a draft flight reply and decide whether it needs
  revision.
- **Output:** `ResponseCritique { needs_revision, issues, confidence }`.
- **Temperature:** 0. Strict reviewer voice.
- **CoT placement:** none. The decision rule is explicit: critical
  issues only (factual accuracy, missing relaxation note, multiple
  questions). Style preferences don't trigger revision.
- **Why this exists:** the responder is the only node where the model
  can hallucinate flight details (citation verifier covers RAG; the
  flight tool is the source of truth for flight data, but the responder
  has freedom in how it phrases things). The critique loop catches
  fabrication before the user sees it.
- **A/B-testable:** controlled by `RESPONDER_SELF_CRITIQUE` env flag.
  The eval suite can run with and without to measure the impact.
  Disabled by default so the baseline cost stays predictable.

---

## Shared conventions (`_shared/persona.md`, `_shared/safety.md`)

These two files document conventions inlined into each operational
prompt:

- **Persona** — voice rules, tone scaling per node, anti-patterns to
  avoid (no "Great choice!", no marketing language, lists for 2+ items,
  etc.)
- **Safety** — refusal phrasing, prompt-injection handling, honesty
  conventions ("I don't have that info" is always available; one
  question at a time; no fake authority)

They're reference docs, not loaded prompts. Each operational prompt
incorporates the conventions natively rather than including the shared
files at runtime — keeps every prompt self-readable in isolation, which
matters during eval iteration when you're staring at one prompt at a
time.
