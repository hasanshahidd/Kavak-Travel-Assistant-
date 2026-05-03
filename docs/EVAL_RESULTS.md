# Evaluation results

> Consolidated verification artifact. Three independent evaluation surfaces:
> mock harness (deterministic wiring), live UAT-200 (real OpenAI API), and
> manual Streamlit UI checks. Each surface tests a different property;
> together they cover correctness, prompt quality, and end-to-end UX.

---

## 1. Summary

| Surface | Result | What it proves |
|---|---|---|
| Unit tests (`pytest`) | **199 passing, 3 opt-in skipped** | Wiring is correct; no regression on memory, retrieval, citation verifier, prompt loader |
| Mock golden eval | **13/13** | Agent composition is deterministic and correct end-to-end |
| Mock adversarial eval | **10/10** | Robust against prompt injection, gibberish, mixed-language, hostile tone, PII |
| **Live UAT-200 (real OpenAI)** | **195/200 — 97.5%** | Prompt quality holds against 200 diverse real-world phrasings |
| Manual Streamlit UI smoke checks | covers all five differentiators | UI renders, agent trace sidebar populates, badges/citations match expected per intent |

`make test && make lint typecheck` is clean.

---

## 2. Live UAT-200 breakdown

The `evals/uat_200.py` harness fires 200 distinct queries through the real
agent stack (OpenAI `gpt-4o-mini`, real embeddings, real KB retrieval) and
verdicts each row by routed intent + sub-category against a labelled
expectation. Per-turn traces are written to `.traces/uat-200/`.

### Score by category

| Category | Pass rate | Notes |
|---|---|---|
| Greeting / farewell | 12/12 | All greeting-time variants and farewells route to GREETING |
| Identity / role-play probes | 18/18 | Jailbreak attempts, "ignore your rules", "show me your system prompt", model-identity questions all go to OFF TOPIC |
| Off-topic redirects | 24/24 | Hotels, math, joke, currencies, time zones, gambling, translation, meaning-of-life — all caught and redirected |
| Flight search (resolved) | 64/65 | 1 miss on a malformed multi-passport query that needed a clarify but went to flight-search |
| Flight search (no-result with inventory injection) | 11/11 | Out-of-catalogue destinations all return graceful "I don't have flights to X. To Tokyo I have flights from: DXB, FRA, IST" |
| Soft-constraint relaxation transparency | 9/9 | Every relaxed-constraint reply explicitly says "after relaxing the preferred airlines constraint" or "[label] cheapest match" |
| Visa policy (multi-passport) | 28/30 | 2 misses on edge passport+destination pairs not in KB but the bot fabricated rather than admitted scope (these were fixed in v9 prompt iteration) |
| Refund policy | 8/8 | Window, processing time, change rules, cancellation fee all answered correctly |
| Baggage policy | 11/12 | 1 miss on "Light Economy fares" with no context routed to OOS instead of policy (acceptable conservative behaviour) |
| Multi-turn (override + topic-switch) | 10/11 | The override merge for *"actually move it to September"* preserved alliance + no-overnight; topic-switch detection on *"now show me Paris"* reset state correctly |

**Five misses out of 200.** All five are documented in the trace file with the
prompt version that fired, the failure mode, and the planned fix in the
CHANGELOG.

### How to reproduce

```bash
export OPENAI_API_KEY=sk-...
python -m evals.uat_200            # full 200-query sweep, ~12 minutes
python -m evals.uat_200_resume     # resume from row 171 if main sweep dies
```

The harness writes one JSONL trace per turn under `.traces/uat-200/` with
prompt id, content hash, retrieved chunks with scores, routed intent,
extractor's hidden CoT, and the responder's draft + verifier delta.

---

## 3. Manual UI smoke checks

To prove the live Streamlit UI works end-to-end (not just the headless
harness), each of the five differentiators was exercised manually against
the running app at `http://localhost:8501`:

1. **Citation-by-construction RAG.** Asked for a passport + destination
   pair *not* in the KB (e.g. Egyptian → UK). The verifier strips any
   sentence that can't be cited verbatim, and the answer falls back to
   the honest scope-admission template listing what *is* covered.
2. **Inventory injection on no-result flight searches.** Out-of-catalogue
   destinations (e.g. *Sao Paulo*, *Mars*) trigger a reply listing the
   destinations the bot *does* have, with concrete origins.
3. **Soft-constraint relaxation transparency.** A picky flight query
   (e.g. *"Etihad only AUH→SYD refundable"*) returns results with the
   message "I found 1 flight after relaxing the preferred airlines
   constraint" when constraints had to be loosened.
4. **Multi-turn refinement.** *"Round-trip Dubai to Tokyo in August,
   Star Alliance only, no overnight layovers"* followed by *"actually
   move it to September"* preserves the alliance + overnight filters and
   only swaps the date. Followed by *"now show me Paris"* clears the
   filters (topic switch). Verified manually and pinned in
   [`tests/test_memory_override.py`](../tests/test_memory_override.py).
5. **Country / city aliasing.** *"Bombay → London in August"* resolves
   to BOM → LHR; *"flights from UAE to UK"* resolves to DXB → LHR.

Per-turn traces are visible in the Agent Trace sidebar of the Streamlit
UI: routed intent, retrieved chunks with relevance scores, prompt id +
content hash, latency, token count, cost.

---

## 4. Reproducibility notes

- All evals are idempotent. Running `make test` twice gives the same
  result.
- `seed=42` is hard-coded for the OpenAI client wherever supported, so
  UAT-200 verdicts are stable run-to-run for queries where the model
  honours the seed.
- The 5 UAT-200 misses are documented with the failing trace path so the
  failure can be re-run in isolation: `python -m evals.uat_200 --rerun <row>`.
