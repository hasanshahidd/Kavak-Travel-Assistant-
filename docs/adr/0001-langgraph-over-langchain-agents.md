# ADR 0001 - LangGraph over LangChain agent executor

**Date:** 2026-05-02
**Status:** Accepted

## Context

The spec says "incorporate LangChain or LangGraph". Both can drive a
travel assistant. They have different ergonomics:

* **LangChain `AgentExecutor`** - opinionated tool-calling loop, ReAct-style.
  The model decides which tool to call next based on a system prompt; the
  framework parses the model's text and routes accordingly.
* **LangGraph `StateGraph`** - explicit state-machine. The developer wires
  up nodes and conditional edges; the model's only role is the inference
  inside each node.

The agent we need has a small, deterministic intent fan-out (flight,
policy, clarify, out-of-scope) and a few well-known sub-flows
(extractor → clarifier vs. flight_search; retriever → answerer →
verifier). The routing decision is taken once per turn by a router prompt
that classifies into one of four intents.

## Decision

Use **LangGraph** for orchestration. Keep node implementations as pure
functions of `(inputs) -> typed-output` and adapt them to LangGraph's
`(state) -> dict[update]` shape in [`builder.py`](../../app/graph/builder.py).

## Why

1. **Explicit topology beats implicit routing.** With LangGraph, the
   topology is a 50-line file ([`builder.py`](../../app/graph/builder.py))
   that a reviewer can read in one screen. With AgentExecutor, the
   routing logic lives inside the model's head and tool-calling loops -
   harder to inspect, test, or debug.

2. **Conditional edges make memory + clarification trivial.** Branching
   on `state["flight_query"].needs_clarification` to route extractor
   output back to the clarifier is one line in LangGraph. In AgentExecutor
   this would be a custom tool-call that the model has to remember to
   make, which is fragile.

3. **Pure-function nodes are unit-testable in isolation.** Each node in
   `app/graph/nodes/*` takes its dependencies as parameters. The
   integration test ([`test_full_agent_integration.py`](../../tests/test_full_agent_integration.py))
   exercises the whole graph; per-node tests cover edge cases without
   booting the full state machine.

4. **The trace is graph-shaped.** Every node emits a trace event. Reading
   them back in order reconstructs the agent's decisions. With agent
   executor the trace is a flat tool-call log - same data, less structure.

## Alternatives considered

* **LangChain AgentExecutor** - rejected per the analysis above. ReAct loops
  are excellent when you don't know the toolset in advance, but our four
  intents are known.
* **Hand-rolled routing** (no framework) - tempting for a small project.
  Rejected because the spec specifically names LangChain or LangGraph,
  and LangGraph's `StateGraph` is small enough that "use a framework"
  isn't a complexity overhead here.
* **Multi-agent with delegation** - overkill. Single-agent with branching
  is the right complexity level for the scope.

## Consequences

**Positive:**
- Topology is inspectable, testable, and easy to reason about
- Per-node trace events have clear semantics
- Conditional branching (especially extractor → clarifier) is one line
- DI-friendly: `build_agent(substrate)` takes mocks for tests, real clients in production

**Negative:**
- One more dependency than a hand-rolled state machine
- LangGraph's `StateGraph(AgentState)` requires runtime-resolvable type hints
  (we hit this once during Block 6 and pulled `TYPE_CHECKING` imports out
  of [`app/graph/state.py`](../../app/graph/state.py))

**Operational note:** LangGraph compiles to a `CompiledGraph` callable;
the same compiled object is reused across turns in the CLI and Streamlit
entry points, so node closures (which bind the substrate) are created
once per process.
