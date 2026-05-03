# Architecture

> Filled out in Block 9 with full per-node contracts. This file is the living architecture reference.

## Overview

The assistant is a **single Python process**. There is no client/server split: `main.py` (CLI) and `streamlit_app.py` (web UI) both import and run the same in-process LangGraph agent.

```
┌─────────────────────┐        ┌─────────────────────┐
│   main.py (CLI)     │───┐    │ streamlit_app.py    │───┐
│   Rich-formatted    │   │    │ Web UI + sidebar    │   │
│   REPL              │   │    │ trace inspector     │   │
└─────────────────────┘   │    └─────────────────────┘   │
                          ▼                              ▼
            ┌─────────────────────────────────────────────────┐
            │                  app/  package                   │
            │                                                  │
            │   ┌──────────────────────────────────────────┐  │
            │   │           LangGraph agent                 │  │
            │   │  router → extractor → flight_search      │  │
            │   │         ↓                                 │  │
            │   │      retriever → answerer → responder    │  │
            │   └──────────────────────────────────────────┘  │
            │                                                  │
            │   prompts/  (versioned .md + CHANGELOG)         │
            │   tools/    (flight_index + FAISS retriever)    │
            │   llm/      (client + tracing + verifier)       │
            │   memory/   (filter memory + override semantics)│
            │   schemas/  (Pydantic v2 contracts)             │
            └─────────────────────────────────────────────────┘
                                │
                                ▼
                  ┌──────────────────────────┐
                  │  data/                   │
                  │  ├── flights.json        │
                  │  ├── airports.json       │
                  │  └── kb/  (markdown KB)  │
                  └──────────────────────────┘
```

## Per-turn flow

```mermaid
sequenceDiagram
    participant U as User
    participant UI as CLI / Streamlit
    participant G as LangGraph
    participant L as LLM
    participant V as FAISS
    participant CV as CitationVerifier

    U->>UI: types message
    UI->>G: agent.invoke(state)
    G->>L: router (intent classification)
    alt intent = flight_search
        G->>L: extractor (NL → FlightQuery)
        G->>G: flight_index.search()
    else intent = policy_qa
        G->>V: retriever top-k=4
        G->>L: answerer (compose RagAnswer)
        G->>CV: verify citations
    else intent = clarify
        G->>L: clarifier (ask one question)
    end
    G->>L: responder (final user-facing reply)
    G-->>UI: AgentState + trace events
    UI-->>U: rendered reply (+ sidebar trace in Streamlit)
```

## Graph topology

```mermaid
flowchart LR
    START([start]) --> R[router]
    R -->|flight_search| E[extractor]
    R -->|policy_qa| RT[retriever]
    R -->|out_of_scope| RS[responder]
    E -->|needs_clarification| C[clarifier]
    E --> FS[flight_search]
    FS --> RS
    RT --> A[answerer]
    A --> CV[citation verifier]
    CV --> RS
    C --> END([end])
    RS --> END
```

## State

```python
class AgentState(TypedDict, total=False):
    messages: list[Message]            # full conversation
    summary: str                        # rolling summary of older turns
    intent: Intent                      # router output
    flight_query: FlightQuery | None    # extracted + memory-merged
    flight_results: list[FlightResult]
    retrieved_chunks: list[Chunk]
    rag_answer: RagAnswer | None
    final_answer: str
    trace_events: list[TraceEvent]      # populated for sidebar
```

## Why this architecture

- **Single process** - one `pip install` and one command runs everything. No client/server coordination, no CORS, no SSE wiring.
- **LangGraph state machine** - intents have clear branches; LangGraph makes the graph explicit and inspectable. We chose this over LangChain's agent executor because the latter hides the routing logic in tool-calling loops, which is harder to reason about and debug.
- **Citation verifier as a graph node** - RAG hallucination is structurally impossible: any unverified claim is stripped before reaching the responder.
- **In-process trace capture** - every node emits `TraceEvent`s into state; the Streamlit sidebar renders them live without any extra plumbing.
