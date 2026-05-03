# Sample transcripts

> Five annotated walkthroughs. Each transcript shows the user message,
> the agent's reply, and the trace highlights that explain *why* the
> agent did what it did.
>
> These are produced by `python main.py --demo`; reviewer can reproduce
> by setting `OPENAI_API_KEY` and running the demo command.

---

## Transcript 1 - Flight search with multiple constraints

```
You: Round-trip Dubai to Tokyo in August, Star Alliance only, no overnight layovers.

Bot (✈️ flight_search):

I found 2 Star Alliance round-trip options without overnight layovers:

1. **Turkish Airlines · DXB → NRT**
   2026-08-15 → 2026-08-30 · $950 · refundable
   5.5h via IST (daytime)
   *Cheapest Star Alliance option without overnight transit.*

2. **Singapore Airlines · DXB → NRT**
   2026-08-18 → 2026-09-02 · $1220 · refundable
   4.0h via SIN (daytime)
   *Shortest layover; comfortable transit point.*

Want me to filter by price?
```

**Trace highlights:**
- `router` → `flight_search` (rationale: "user mentioned Dubai, Tokyo, August, Star Alliance - all flight signals")
- `extractor` → `FlightQuery(origin="Dubai", destination="Tokyo", preferred_alliances=["Star Alliance"], avoid_overnight_layovers=True, ...)`. The scratchpad records *"avoid overnight is a NEGATION; do NOT add as positive layover filter"*.
- `flight_search` → `relaxed_constraints=[]` (exact match, no relaxation)
- `responder.draft` only (self-critique disabled by default; the env-flag toggles it)

---

## Transcript 2 - Policy Q&A with verified citation

```
You: Do UAE passport holders need a visa for Japan for tourism?

Bot (📄 policy_qa):

UAE passport holders can enter Japan visa-free for tourism for up to 30 days,
provided their passport is valid for at least 6 months from arrival, and proof
of onward travel may be requested at immigration.

Source: visa_rules.md · "UAE passport - Japan"
```

**Trace highlights:**
- `router` → `policy_qa` (rationale: "passport + visa keywords")
- `retriever` → top chunk `visa_rules.md#uae-passport-japan` with score `0.84`; threshold `0.5` cleared
- `answerer` → `RagAnswer` with one citation; verifier passed (`citations_kept=1, citations_stripped=0`)
- Confidence: `0.9`

---

## Transcript 3 - Multi-turn refinement (override + topic switch)

```
You (turn 1): Round-trip Dubai to Tokyo in August, Star Alliance only, no overnight.
Bot:           [returns 2 matches as in Transcript 1]

You (turn 2): actually move it to September.
Bot (✈️ flight_search):

Updated to September 2026. Same destination, same alliance preference,
no overnight layovers - found 1 Star Alliance match:

1. **Turkish Airlines · DXB → NRT**
   2026-09-12 → 2026-09-27 · $1080 · refundable
   5.5h via IST (daytime)
   *Same route, dates moved to September.*

You (turn 3): now show me flights to Paris.
Bot (✈️ flight_search):

New search - Dubai → Paris. Previous Star Alliance and no-overnight
constraints dropped because you switched destinations. I found 3 matches...
```

**Trace highlights - turn 2 (override):**
- `extractor.had_prior=true`, `topic_switch=false`
- The merge filled in destination/alliance/no-overnight from the prior FlightQuery; only `departure_date` was changed by the model

**Trace highlights - turn 3 (topic switch):**
- `extractor.had_prior=true`, `topic_switch=true`
- Merge returned the new query unchanged - old preferences were dropped
- The responder's reply explicitly notes the constraints were dropped (transparency)

**Why this matters:** state leak across turns is the #1 multi-turn failure
mode in conversational flight search. The integration test
[`test_full_agent_integration.py::test_five_turn_conversation_preserves_state_correctly`](../tests/test_full_agent_integration.py)
proves this works.

---

## Transcript 4 - Clarification path (origin missing)

```
You: flights to Bali next month under $700

Bot (❓ clarify):

I see you want flights to Bali next month under $700 - what city are you flying from?
```

**Trace highlights:**
- `router` → `flight_search`
- `extractor` → `FlightQuery(destination="Bali", departure_date="2026-06-01", max_price_usd=700, needs_clarification=true, missing_fields=["origin"])`
- Conditional edge: `needs_clarification=true` → branch to `clarifier` (NOT `flight_search`)
- `clarifier` → single question per the prompt's one-question rule. Trace records `missing_fields=["origin"]` so the question targets the most important field first.

---

## Transcript 5 - Graceful refusal

```
You: What are the visa requirements for Atlantis?

Bot (📄 policy_qa):

I don't have information about that in my knowledge base. The policies I
do have cover visa rules, refund policy, and baggage policy for a small
set of common routes.
```

**Trace highlights:**
- `router` → `policy_qa`
- `retriever` → 0 chunks above threshold (relevance gate fired)
- `answerer.path = "structural_refusal"` - **no LLM call made**, zero tokens consumed
- This is the anti-hallucination architecture working as designed: when retrieval finds nothing relevant, the agent refuses without calling the model. There's no opportunity for hallucination because the model never sees the question.

---

## Transcript 6 - Out-of-scope redirect

```
You: What's the weather like in Tokyo right now?

Bot (⛔ out_of_scope):

That's outside what I can help with right now - I focus on flight
search and travel-policy questions (visa, refund, baggage). Want me to
look up flights or check a policy instead?
```

**Trace highlights:**
- `router` → `out_of_scope` (rationale: "weather is not in our scope")
- `out_of_scope` node returns canned text, **no LLM call**, zero tokens, zero cost
- The redirect is positive (suggests in-scope alternatives) rather than just refusing - borrowed from the conventions in [`_shared/safety.md`](../app/prompts/_shared/safety.md)

---

## How to reproduce

```bash
OPENAI_API_KEY=sk-... python main.py --demo
```

The CLI runs through all 5 turns of the canonical multi-turn scenario
and prints the trace from the last turn so you can verify the path
each turn took.

For a more interactive walkthrough, run `streamlit run streamlit_app.py`
and ask the queries above one by one - the sidebar shows the live
trace per turn.
