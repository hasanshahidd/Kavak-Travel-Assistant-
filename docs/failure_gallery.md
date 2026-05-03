# Failure gallery

> Three honest failure modes with the recovery designed for each.
> Showing what the bot can't do and how it fails gracefully is a deliberate
> design choice — silent best-effort behaviour is the worst outcome a
> travel-policy assistant can have.

---

## Failure 1 — Mock embeddings can't reliably surface the right KB chunk

### What goes wrong
The mock embeddings client is a deterministic hash-based bag-of-words
(see [`app/llm/embeddings.py`](../app/llm/embeddings.py) — `MockEmbeddingsClient`).
It exists so the demo runs offline. For short queries with sparse
vocabulary overlap, the cosine similarity score against the right KB
chunk can fall below the 0.5 relevance threshold.

### What the user sees

```
You: visa for japan?

Bot: 📄 Policy Q&A

I don't have information about that in my knowledge base. The policies
I do have cover visa rules, refund policy, and baggage policy for a
small set of common routes.
```

### Why this is acceptable
- The retrieval threshold gate is doing its job: when relevance is too
  low, the answerer takes the structural-refusal path instead of
  hallucinating.
- **Real OpenAI embeddings (text-embedding-3-small) handle short
  queries fine** — this only manifests in mock mode.
- The refusal phrasing tells the user what topics ARE covered, so they
  can rephrase: *"Do UAE passport holders need a visa for Japan?"*
  retrieves correctly even with mock embeddings.

### Trace evidence
The trace's `retriever` event records `below_threshold: true` and the
chunk-score histogram. A reviewer can confirm in one read that the
agent didn't even call the LLM for the answer (zero tokens consumed),
which is the right behaviour.

---

## Failure 2 — Single-turn typo in destination

### What goes wrong
User types *"flights to Tokio"* (typo for Tokyo). The IATA alias map
in [`data/airports.json`](../data/airports.json) doesn't include "Tokio"
as an alias.

### What the user sees

```
You: round-trip from Dubai to Tokio in August

Bot: ✈️ Flight search

I don't have flights to 'Tokio' in my dataset. Try a major
international destination.
```

### Why this is acceptable
The diagnostic is more useful than a silent empty result. The bot
explicitly tells the user *what it didn't recognise*, which gives them
the information needed to retry — instead of producing zero matches
and leaving the user to guess what went wrong.

### What we could do better
- A fuzzy-match fallback ("did you mean Tokyo?") — out of scope for
  the project but a one-line addition to `app/utils/airports.py`.
- Currently the only typo-tolerance is the alias map (Bombay → BOM,
  NYC → JFK/EWR/LGA). Real production would add an edit-distance
  fallback or a small LLM "did you mean" pass.

---

## Failure 3 — Conflicting hard constraints

### What goes wrong
User specifies constraints that no flight in the dataset can satisfy,
where the conflicting constraint is **hard** (price, refundability,
date) — soft-constraint relaxation can't help because relaxation only
drops *soft* preferences.

### What the user sees

```
You: cheapest direct flight to Sydney under $200

Bot: ✈️ Flight search

No flights to that route/date under $200. Want me to look at slightly
higher prices?
```

### Why this is acceptable
- The diagnoser ([`app/tools/flight_index.py`](../app/tools/flight_index.py),
  `_diagnose_hard_failure`) tries selectively unsetting hard
  constraints to identify *which* one blocked the search, then
  surfaces a useful follow-up question.
- If we'd silently relaxed price and shipped a $400 flight, the user
  would either feel ignored ("I said under $200") or trust the bot
  less.
- Surfacing the constraint that blocks search is honest *and*
  actionable — the user can either accept the relaxation or pick a
  different route.

### Trace evidence
The trace's `flight_search` event records `result_count: 0`,
`is_relaxed: false`, and `no_results_reason: "No flights to that
route/date under $200. ..."`. The responder uses this verbatim, which
is why the wording surfaces honestly to the user instead of being
re-phrased by the model.

---

## What ties these failures together

Each one demonstrates a designed property:

- **Failure 1** — relevance threshold gate prevents hallucination by
  forcing refusal when retrieval is weak.
- **Failure 2** — alias-map gap surfaces honestly with a useful error
  message rather than silently returning empty results.
- **Failure 3** — hard-constraint conflicts get diagnosed by the tool
  and surfaced as actionable follow-ups, not silently relaxed.

The unifying principle: **silent best-effort matching is worse than an
honest "I can't"**. Every failure mode in this gallery is the
intentional result of choosing transparency over false confidence.
