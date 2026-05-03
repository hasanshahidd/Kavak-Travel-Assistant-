# Scope decisions: data sizing & coverage

> Why the catalogue and KB are the size they are. What's deliberately in
> scope, what's deliberately out, and what would change if this went to
> production.

---

## 1. Why this document exists

A travel assistant is only as good as the data it's grounded in. Two
common failure modes for a case-study submission:

1. **Catalogue is too thin** — the bot says "no flights matched" for
   plausible queries because the catalogue doesn't include the route, not
   because the agent is wrong. Reviewer can't tell the difference.
2. **Catalogue is too sprawling** — the bot looks impressive on demo
   queries but the answers are unverifiable and the data has no internal
   consistency.

This project sized the data deliberately to land between those two
failure modes. The matrix below is the contract: every cell either has
data, or has a documented reason for not having data.

---

## 2. Flight catalogue: 90 flights, 7 origins, 11 destinations, 8 months

The catalogue lives in [`data/flights.json`](../data/flights.json) and is
generated/expanded by [`scripts/expand_flights.py`](../scripts/expand_flights.py)
(idempotent, re-runnable).

### Origin × destination matrix

|         | LHR | CDG | JFK | SIN | BKK | NRT | FRA | AMS | HKG | SYD | BOM | DEL | AUH |
|---------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **DXB** |  ✓  |  ✓  |  ✓  |  ✓  |  ✓  |  ✓  |  ✓  |  ✓  |  ✓  |  ✓  |  ✓  |  ✓  | —   |
| **AUH** |  ✓  | —   |  ✓  |  ✓  |  ✓  | —   | —   | —   | —   |  ✓  |  ✓  | —   | —   |
| **BOM** |  ✓  | —   |  ✓  |  ✓  |  ✓  | —   | —   | —   |  ✓  | —   | —   | —   |  ✓  |
| **DEL** |  ✓  | —   |  ✓  | —   |  ✓  | —   |  ✓  | —   | —   | —   | —   | —   |  ✓  |
| **IST** |  ✓  | —   |  ✓  | —   | —   |  ✓  | —   | —   | —   | —   | —   | —   | —   |
| **FRA** | —   | —   |  ✓  | —   | —   |  ✓  | —   | —   | —   | —   | —   | —   | —   |

✓ = at least one flight in catalogue.

### Why these origins

- **DXB / AUH** — primary hubs the assistant is positioned around. UAE
  audience is the assumed user base.
- **BOM / DEL** — high-volume India outbound traffic, justifies the
  Indian-passport visa coverage in the KB.
- **IST / FRA** — connecting hubs with their own Star Alliance / SkyTeam
  long-hauls, surface the alliance filter.

### Why these destinations

- **LHR, CDG, JFK, SIN, BKK, NRT** — six high-volume long-hauls; cover
  Europe, North America, SE Asia, Japan.
- **FRA, AMS, HKG, SYD** — secondary destinations that surface specific
  features (alliance variety, overnight layover behaviour, Australia
  routing).
- **AUH, BOM, DEL** as destinations enable regional UAE↔India routing
  and same-region short hauls.

### Why these months

8 months span (Jun 2026 → Jan 2027). Enough to:
- Test month-name resolution (*"in August"* → 2026-08-XX).
- Test cross-year returns (Dec 2026 → Jan 2027).
- Stress the "no flights for that month" path (*"DXB to LHR in May"*
  triggers the inventory-injection reply with months we *do* have).

### What's deliberately out

- South America destinations. The "Sao Paulo" miss in
  `docs/EVAL_RESULTS.md` is **expected** — it triggers the no-flight
  inventory injection rather than fabricating a route.
- African destinations beyond what's listed.
- Multi-stop itineraries beyond a single layover.
- Domestic-only routes.

These would expand naturally for production but each adds maintenance
load (visa coverage for the new region, currency display, market-specific
disclaimers).

---

## 3. Knowledge base: 38 sections across 3 documents

The KB lives in [`data/`](../data/) split across:

- `visa_rules.md` — passport × destination visa requirements (23 sections)
- `refund_policy.md` — cancellation windows, change rules, processing time, fees (8 sections)
- `baggage_policy.md` — class-by-class allowances, restricted items, lost-bag protocol (7 sections)

### Visa coverage matrix

| Passport | Schengen | UK | USA | Japan | Australia | Bangkok |
|----------|:--------:|:--:|:---:|:-----:|:---------:|:-------:|
| UAE      |    ✓     |  ✓ |  ✓  |   ✓   |    —     |   —     |
| Indian   |    ✓     |  ✓ |  ✓  |   ✓   |    ✓     |   ✓     |
| Pakistani|    ✓     |  ✓ |  —  |   ✓   |    —     |   ✓     |
| UK       |    ✓     |  — |  —  |   ✓   |    ✓     |   —     |
| Saudi    |    ✓     |  ✓ |  —  |   —   |    —     |   —     |
| Egyptian |    ✓     |  — |  —  |   —   |    —     |   —     |
| Filipino |    —     |  — |  —  |   ✓   |    —     |   —     |

✓ = section exists in `visa_rules.md`.

### Why these passports

The five major passport groups for UAE-outbound travel (Emirati, Indian,
Pakistani, UK, Saudi) plus two coverage-gap passports (Egyptian,
Filipino) to test the "honest scope admission" path in the answerer.

### What's deliberately out

- Passports not commonly seen in UAE traffic (most African, most
  Caribbean, most South American).
- Long-stay / work / student visa categories — only tourist visas covered.
- Visa fees and processing addresses — easily out of date, deliberately
  excluded.
- Real-time embassy queue lengths or appointment slots.

These exclusions are the answerer's "I don't have information about that
in my knowledge base. The policies I do have cover X, Y, Z" path,
verified live in UAT-200.

---

## 4. What would change in production

If this assistant shipped to real users tomorrow, the data sizing
decisions would change in three ways:

1. **Catalogue would come from a live GDS feed** (Amadeus, Sabre, or
   direct airline NDC). The current static catalogue would become a
   testing fixture only. The agent code wouldn't change — `KBRetriever`
   and `flight_search` are protocol-based.
2. **KB would split per passport-issuing country** with a regional editor
   responsible for keeping each section current. The verifier already
   refuses to cite stale or missing content; the workflow would be:
   editor updates `.md` → CI runs golden eval → deploy.
3. **Coverage matrix would be enforced by CI** — a missing cell in
   either matrix above would fail the build, forcing either a data
   addition or an explicit `OUT_OF_SCOPE.md` entry.

The matrix view in this document is the artifact that production would
formalise. Right now it's a doc; in production it'd be a YAML file the
build reads.
