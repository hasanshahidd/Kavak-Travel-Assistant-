# ADR 0003 - Spec-compliant flat layout, single-process Python

**Date:** 2026-05-02
**Status:** Accepted

## Context

The submission spec mandates this exact root structure:

```
main.py
data/
  flights.json
  visa_rules.md
requirements.txt
README.md
(optional) streamlit_app.py
```

with the warning: *"Incomplete content can risk the job application not
proceeding forward."*

An earlier iteration of this project used a `backend/` + `frontend/`
monorepo with FastAPI + React + SSE streaming. That structure violates
the spec literally - `main.py` is at `backend/main.py`, not at the root.

A flexible reviewer would forgive the deviation. A by-the-book reviewer
running through a checklist would mark "structure: not compliant" and
move on.

## Decision

**Restructure to the spec's exact flat layout.** Drop the React frontend
and FastAPI server. Replace with:

* `main.py` at the repo root - Rich-formatted CLI entry
* `streamlit_app.py` at the repo root - web UI with a live trace sidebar
* The agent itself as a Python *package* (`app/`) imported by both
  entry points - not a separate process

## Why

1. **Spec compliance is non-negotiable.** The asymmetric risk is
   one-way: 15 min of restructure cost vs. potential automatic
   rejection.

2. **Single-process Python is simpler in every dimension.**

   | Concern | Two-process (FastAPI + React) | Single-process |
   |---|---|---|
   | Reviewer setup | `cd backend && uvicorn ...` + `cd frontend && npm run dev` | `pip install -r requirements.txt` + `streamlit run streamlit_app.py` |
   | CORS, auth, streaming protocol | Real concerns | None |
   | Debug story | "Which process logged this?" | One log, one tracer |
   | Test wiring | HTTP mocks | Direct calls |

3. **The trace sidebar is *easier* in Streamlit than in React.** The
   sidebar reads `tracer.events` (an in-process Python list) directly.
   With React + FastAPI the same UX needs SSE plumbing, JSON
   serialization, frontend state management - all for the same final
   pixel output.

4. **Every 0.01% differentiator survives the restructure.** Versioned
   prompts, citation verifier, multi-turn override memory, adversarial
   eval, trace replay - none of them depended on having a separate
   server process.

## Trade-offs we accept

- **No HTTP API surface.** A future client (mobile app, Slack bot)
  would need either a thin FastAPI wrapper or a separate adapter.
  Acceptable for a portfolio submission; documented here so it's not
  forgotten if the project grows.
- **Streamlit's threading model** is what we're stuck with. For
  high-concurrency use, FastAPI + worker processes would be better.
  Not a concern for a single-reviewer demo.
- **Frontend polish ceiling is Streamlit's.** A custom React UI could
  look slicker. Streamlit looks polished enough that this isn't a
  reviewer-perceptible loss; the *content* of the trace sidebar is the
  unique thing, not the styling.

## Alternatives considered

* **Keep the monorepo, hope the reviewer is flexible.** Rejected - the
  asymmetric risk is too high.
* **Symlink hack** (real code in `backend/`, symlinks at the root).
  Rejected - works on Linux/Mac but breaks on Windows reviewers, and
  is the kind of "clever" that reads as "I didn't want to do the
  refactor."
* **Hybrid: spec files at root + `backend/` shim that re-exports
  everything.** Rejected - adds complexity for no real upside; the
  flat layout is genuinely cleaner.

## Consequences

**Positive:**
- Spec compliance is locked in (`main.py`, `data/flights.json`,
  `data/visa_rules.md`, `requirements.txt`, `README.md`,
  `streamlit_app.py` all at root)
- Reviewer setup is one command
- Single trace stream, single log, single failure surface
- About 1.5 hours saved (no React build step) reinvested into the
  prompt CHANGELOG and the eval suite

**Negative:**
- A future REST API requires re-introducing FastAPI
- We lose the "I built a full-stack app in 4 hours" optionality

**Confidence-builder:** the connectivity audit that ran after the
restructure (see Block 4 conversation) confirmed every shipped file
imports from or is imported by another shipped file. No dead code, no
orphaned modules, no stale references to the old layout.
