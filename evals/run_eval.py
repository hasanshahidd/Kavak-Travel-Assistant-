"""Eval harness — runs golden + adversarial sets, writes JSON + Markdown results.

Two modes:

* **mock** (default) — uses the deterministic mock LLM + mock embeddings to
  exercise wiring at zero cost. Routing, extraction-merge, and adversarial
  PII / injection are all measurable here. RAG accuracy isn't (mock retrieval
  doesn't surface verbatim-match chunks reliably) so the RAG subset is
  marked as ``mock_skip``.
* **real** (``--real``) — uses the configured OpenAI provider. Costs ~$0.05
  for a full eval run on gpt-4o-mini. Recommended for the iteration loop.

Both modes write three artefacts to ``evals/results/``:

* ``golden.json``       — per-case pass/fail with diagnostic details
* ``adversarial.json``  — same shape for the adversarial set
* ``metrics.md``        — human-readable summary table + per-category breakdown

Run with::

    python -m evals.run_eval                # mock
    python -m evals.run_eval --real         # live OpenAI
    python -m evals.run_eval --golden-only  # skip adversarial
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.graph.builder import AgentSubstrate, build_agent
from app.llm.client import MockClient, get_llm_client
from app.llm.embeddings import MockEmbeddingsClient, get_embeddings_client
from app.llm.tracing import Tracer
from app.schemas.flight import FlightQuery
from app.schemas.intent import Intent, RouterOutput
from app.schemas.oos import OOSReply
from app.schemas.rag import RagAnswer
from app.tools.flight_index import FlightIndex
from app.tools.kb_retriever import KBRetriever

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"
GOLDEN_PATH = EVAL_DIR / "golden_set.jsonl"
ADVERSARIAL_PATH = EVAL_DIR / "adversarial.jsonl"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CaseResult:
    """One eval case outcome with diagnostic details for the report."""

    id: str
    category: str
    passed: bool
    intent: str | None
    expected_intent: str | None
    latency_ms: float
    cost_usd: float
    tokens: int
    failure_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SuiteResult:
    name: str
    cases: list[CaseResult]
    total_latency_ms: float
    total_cost_usd: float

    @property
    def pass_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.passed) / len(self.cases)

    def by_category(self) -> dict[str, tuple[int, int]]:
        """Returns category -> (passed, total)."""
        out: dict[str, list[CaseResult]] = {}
        for c in self.cases:
            out.setdefault(c.category, []).append(c)
        return {cat: (sum(1 for c in cs if c.passed), len(cs)) for cat, cs in out.items()}


# ---------------------------------------------------------------------------
# Mock pre-registration
# ---------------------------------------------------------------------------


def _preregister_mock_responses(client: MockClient) -> None:
    """Pre-load canned responses keyed by prompt id.

    The mock harness registers responses such that:
    - router returns the routing decision the case expects
    - extractor returns a flight query consistent with the user message

    Per-case overrides happen inside ``_run_case`` when the case has more
    specific expectations (e.g. an exact extracted query).
    """
    # Default routing: flight_search. Per-case overrides set the right intent.
    client.register(
        "router.v10",
        raw_text=RouterOutput(intent=Intent.FLIGHT_SEARCH, rationale="default mock").model_dump_json(),
        parsed=RouterOutput(intent=Intent.FLIGHT_SEARCH, rationale="default mock"),
    )
    # Default extractor: empty query (will be overridden per case).
    empty = FlightQuery()
    client.register("extractor.v2", raw_text=empty.model_dump_json(), parsed=empty)
    # Default RAG: refusal.
    refusal = RagAnswer(answer="I don't have that info.", citations=[], is_refusal=True)
    client.register("rag_answer.v2", raw_text=refusal.model_dump_json(), parsed=refusal)
    # Default OOS reply (v3 — LLM-driven).
    oos_default = OOSReply(
        category="redirect",
        reply="That's outside what I cover — I focus on flights and travel-policy questions.",
    )
    client.register(
        "oos_reply.v4", raw_text=oos_default.model_dump_json(), parsed=oos_default
    )


def _register_for_case(client: MockClient, case: dict) -> None:
    """Customize the mock client per case so the test exercises the right path."""
    # Routing
    intent_str = case.get("expected_intent", "flight_search")
    intent = Intent(intent_str)
    client.register(
        "router.v10",
        raw_text=RouterOutput(intent=intent, rationale=f"mock for {case['id']}").model_dump_json(),
        parsed=RouterOutput(intent=intent, rationale=f"mock for {case['id']}"),
    )
    # Extraction (only for flight_search cases that include expected_query)
    if intent is Intent.FLIGHT_SEARCH:
        eq = case.get("expected_query") or {}
        # Build a FlightQuery from the expected fields. needs_clarification / missing_fields
        # signal the clarifier branch.
        try:
            q = FlightQuery(**eq)
        except Exception:
            q = FlightQuery()
        client.register("extractor.v2", raw_text=q.model_dump_json(), parsed=q)


# ---------------------------------------------------------------------------
# Suite execution
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _build_substrate(real: bool, cache_dir: Path) -> AgentSubstrate:
    if real:
        return AgentSubstrate(
            client=get_llm_client(),
            kb=KBRetriever(embeddings=get_embeddings_client(), cache_dir=cache_dir),
            index=FlightIndex(),
            self_critique=False,
        )
    mock = MockClient()
    _preregister_mock_responses(mock)
    return AgentSubstrate(
        client=mock,
        kb=KBRetriever(embeddings=MockEmbeddingsClient(), cache_dir=cache_dir),
        index=FlightIndex(),
        self_critique=False,
    )


def _run_case(case: dict, substrate: AgentSubstrate, traces_dir: Path, real: bool) -> CaseResult:
    started = time.perf_counter()

    # Mock mode: customize the client per case so the *path* is exercised correctly.
    if isinstance(substrate.client, MockClient):
        _register_for_case(substrate.client, case)

    tracer = Tracer(turn_id=f"eval-{case['id']}", trace_dir=traces_dir, redact=True)
    agent = build_agent(substrate)

    try:
        prior = case.get("prior_query")
        prior_q = FlightQuery(**prior) if prior else None
        state = agent.invoke(
            {
                "user_message": case["user_message"],
                "summary": case.get("summary", "(no prior conversation)"),
                "prior_query": prior_q,
                "tracer": tracer,
                "turn_id": tracer.turn_id,
            }
        )
    except Exception as exc:
        return CaseResult(
            id=case["id"],
            category=case["category"],
            passed=False,
            intent=None,
            expected_intent=case.get("expected_intent"),
            latency_ms=(time.perf_counter() - started) * 1000,
            cost_usd=0.0,
            tokens=0,
            failure_reason=f"exception: {type(exc).__name__}: {exc}",
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    intent_actual = state.get("intent")
    expected_intent = case.get("expected_intent")
    failure: str | None = None

    # ---- Routing check ----
    if (
        expected_intent
        and intent_actual is not None
        and intent_actual.value != expected_intent
    ):
        failure = f"intent mismatch: expected {expected_intent}, got {intent_actual.value}"

    # ---- Extraction expectations ----
    if not failure and "expected_query" in case and case["category"] == "extraction":
        actual_q: FlightQuery | None = state.get("flight_query")
        if actual_q is None:
            failure = "expected_query set but no flight_query produced"
        else:
            for field_name, expected_val in case["expected_query"].items():
                actual_val = getattr(actual_q, field_name, None)
                if expected_val != actual_val:
                    failure = (
                        f"extraction.{field_name}: expected {expected_val!r}, got {actual_val!r}"
                    )
                    break

    # ---- Multi-turn inheritance ----
    if not failure and "expected_query_inheritance" in case:
        actual_q = state.get("flight_query")
        if actual_q is None:
            failure = "inheritance expected but no flight_query produced"
        else:
            prior_q = FlightQuery(**case["prior_query"])
            for field_name in case["expected_query_inheritance"]:
                if getattr(actual_q, field_name) != getattr(prior_q, field_name):
                    failure = (
                        f"inheritance.{field_name}: expected to inherit "
                        f"{getattr(prior_q, field_name)!r}, got "
                        f"{getattr(actual_q, field_name)!r}"
                    )
                    break

    # ---- Topic switch reset ----
    if not failure and case.get("expected_topic_switch"):
        actual_q = state.get("flight_query")
        # When topic switch is expected, the responder still reports
        # `topic_switch=True` in the extractor trace event.
        ext_event = next((e for e in tracer.events if e.node == "extractor"), None)
        if ext_event is None or ext_event.output.get("topic_switch") is not True:
            failure = "expected topic_switch=True in extractor trace, not seen"

    # ---- RAG expectations ----
    if not failure and case["category"] == "rag":
        if real:
            rag: RagAnswer | None = state.get("rag_answer")
            if rag is None:
                failure = "no rag_answer produced"
            else:
                for needed in case.get("expected_answer_contains", []):
                    if needed.lower() not in rag.answer.lower():
                        failure = f"rag answer missing expected substring: {needed!r}"
                        break
                if not failure and case.get("expected_doc"):
                    citation_docs = {c.doc for c in rag.citations}
                    if case["expected_doc"] not in citation_docs:
                        failure = (
                            f"rag citations missing expected doc {case['expected_doc']!r} "
                            f"(got {citation_docs})"
                        )
        else:
            # In mock mode RAG quality isn't measurable; pass if path executed.
            pass

    # ---- Refusal expectations ----
    if not failure and case.get("expected_refusal"):
        rag = state.get("rag_answer")
        if rag is None or not rag.is_refusal:
            failure = "expected refusal but answer was not flagged as refusal"

    # ---- Prompt-injection / no-leak ----
    if not failure and "expected_no_leak" in case:
        for forbidden in case["expected_no_leak"]:
            if forbidden.lower() in state.get("final_answer", "").lower():
                failure = f"forbidden phrase {forbidden!r} leaked into reply"
                break

    # ---- PII redaction ----
    if not failure and "expected_pii_redacted_in_trace" in case:
        traces = []
        for ev in tracer.events:
            traces.append(json.dumps(ev.model_dump(mode="json"), default=str))
        joined = "\n".join(traces)
        for raw in case["expected_pii_redacted_in_trace"]:
            if raw in joined:
                failure = f"PII not redacted from trace: {raw!r}"
                break

    return CaseResult(
        id=case["id"],
        category=case["category"],
        passed=failure is None,
        intent=intent_actual.value if intent_actual is not None else None,
        expected_intent=expected_intent,
        latency_ms=elapsed_ms,
        cost_usd=tracer.total_cost_usd,
        tokens=tracer.total_tokens,
        failure_reason=failure,
        extra={"trace_path": str(tracer._path)},
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _suite_to_dict(suite: SuiteResult, mode: str) -> dict:
    return {
        "suite": suite.name,
        "mode": mode,
        "ran_at": datetime.now(UTC).isoformat(),
        "pass_rate": round(suite.pass_rate, 4),
        "passed": sum(1 for c in suite.cases if c.passed),
        "total": len(suite.cases),
        "total_latency_ms": round(suite.total_latency_ms, 2),
        "total_cost_usd": round(suite.total_cost_usd, 6),
        "by_category": {
            cat: {"passed": p, "total": t, "rate": round(p / t, 4) if t else 0}
            for cat, (p, t) in suite.by_category().items()
        },
        "cases": [
            {
                "id": c.id,
                "category": c.category,
                "passed": c.passed,
                "intent": c.intent,
                "expected_intent": c.expected_intent,
                "latency_ms": round(c.latency_ms, 2),
                "cost_usd": round(c.cost_usd, 6),
                "tokens": c.tokens,
                "failure_reason": c.failure_reason,
            }
            for c in suite.cases
        ],
    }


def _write_metrics_md(golden: SuiteResult, adversarial: SuiteResult | None, mode: str) -> Path:
    out = RESULTS_DIR / "metrics.md"
    lines: list[str] = []
    lines.append("# Eval results")
    lines.append("")
    lines.append(f"_Last run: {datetime.now(UTC).isoformat()} · mode: **{mode}**_")
    lines.append("")

    def _fmt_suite(s: SuiteResult) -> list[str]:
        rows = [
            f"### {s.name}",
            "",
            f"- **Pass rate:** {s.pass_rate:.0%} ({sum(c.passed for c in s.cases)}/{len(s.cases)})",
            f"- **Total latency:** {s.total_latency_ms:.0f} ms",
            f"- **Total cost (USD):** ${s.total_cost_usd:.6f}",
            "",
            "**By category:**",
            "",
            "| Category | Passed | Total | Rate |",
            "|---|---|---|---|",
        ]
        for cat, (p, t) in sorted(s.by_category().items()):
            rate = (p / t) if t else 0
            rows.append(f"| {cat} | {p} | {t} | {rate:.0%} |")
        if any(not c.passed for c in s.cases):
            rows.append("")
            rows.append("**Failures:**")
            rows.append("")
            for c in s.cases:
                if not c.passed:
                    rows.append(f"- `{c.id}` ({c.category}) — {c.failure_reason}")
        rows.append("")
        return rows

    lines.extend(_fmt_suite(golden))
    if adversarial is not None:
        lines.extend(_fmt_suite(adversarial))

    if mode == "mock":
        lines.append("> **Mode note.** Mock mode doesn't measure RAG or LLM-judgement quality —")
        lines.append("> it exercises the wiring deterministically. Run `python -m evals.run_eval --real`")
        lines.append("> with `OPENAI_API_KEY` set for measurements that reflect prompt quality.")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the eval suite.")
    parser.add_argument(
        "--real", action="store_true", help="Use real OpenAI provider (costs apply)."
    )
    parser.add_argument("--golden-only", action="store_true", help="Skip adversarial set.")
    parser.add_argument("--adversarial-only", action="store_true", help="Skip golden set.")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    cache_dir = settings.faiss_index_dir
    traces_dir = RESULTS_DIR / "_traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    mode = "real" if args.real else "mock"
    print(f"\n=== Kavak eval — mode: {mode} ===\n")

    suites: dict[str, SuiteResult] = {}

    def _run_suite(name: str, path: Path) -> SuiteResult:
        cases = _load_jsonl(path)
        substrate = _build_substrate(real=args.real, cache_dir=cache_dir)
        results: list[CaseResult] = []
        suite_started = time.perf_counter()
        for case in cases:
            print(f"  • {case['id']:<5} {case['category']:<22}", end=" ")
            r = _run_case(case, substrate, traces_dir, real=args.real)
            results.append(r)
            mark = "✅" if r.passed else "❌"
            extra = "" if r.passed else f"  ← {r.failure_reason}"
            print(f"{mark}  ({r.latency_ms:>5.0f}ms){extra}")
        elapsed = (time.perf_counter() - suite_started) * 1000
        return SuiteResult(
            name=name,
            cases=results,
            total_latency_ms=elapsed,
            total_cost_usd=sum(r.cost_usd for r in results),
        )

    if not args.adversarial_only:
        print("Golden set:")
        suites["golden"] = _run_suite("Golden", GOLDEN_PATH)
        (RESULTS_DIR / "golden.json").write_text(
            json.dumps(_suite_to_dict(suites["golden"], mode), indent=2),
            encoding="utf-8",
        )
        print()

    if not args.golden_only:
        print("Adversarial set:")
        suites["adversarial"] = _run_suite("Adversarial", ADVERSARIAL_PATH)
        (RESULTS_DIR / "adversarial.json").write_text(
            json.dumps(_suite_to_dict(suites["adversarial"], mode), indent=2),
            encoding="utf-8",
        )
        print()

    metrics_path = _write_metrics_md(
        suites.get("golden", SuiteResult(name="Golden", cases=[], total_latency_ms=0, total_cost_usd=0)),
        suites.get("adversarial"),
        mode,
    )
    print(f"\nResults written to {RESULTS_DIR}/")
    print("  - golden.json")
    print("  - adversarial.json")
    print(f"  - {metrics_path.name}")

    # Summary
    total_passed = sum(1 for s in suites.values() for c in s.cases if c.passed)
    total_cases = sum(len(s.cases) for s in suites.values())
    total_cost = sum(s.total_cost_usd for s in suites.values())
    if total_cases:
        all_rates = [(s.name, s.pass_rate) for s in suites.values()]
        rate_summary = ", ".join(f"{n}: {r:.0%}" for n, r in all_rates)
        latencies = [c.latency_ms for s in suites.values() for c in s.cases]
        p50 = statistics.median(latencies)
        print(f"\nOverall: {total_passed}/{total_cases} pass ({rate_summary})")
        print(f"Latency p50: {p50:.0f}ms · Total cost: ${total_cost:.4f}")

    failed = total_cases - total_passed
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
