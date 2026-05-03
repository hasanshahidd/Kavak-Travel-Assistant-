"""Tracing tests — PII redaction + JSONL roundtrip + per-turn rollups."""

from __future__ import annotations

from pathlib import Path

from app.llm.tracing import Tracer, read_trace, redact_pii

# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------


def test_redacts_email_in_string() -> None:
    out = redact_pii("Reach me at ahmed.raza+test@example.com for details.")
    assert "[REDACTED_EMAIL]" in out
    assert "@example.com" not in out


def test_redacts_phone_number_in_string() -> None:
    out = redact_pii("Call +971 50 123 4567 to confirm")
    assert "[REDACTED_PHONE]" in out


def test_redacts_passport_pattern() -> None:
    out = redact_pii("Passport: A12345678 issued in 2020.")
    assert "[REDACTED_PASSPORT]" in out
    assert "A12345678" not in out


def test_redacts_card_number_pattern() -> None:
    out = redact_pii("Charge to 4111-1111-1111-1111 please.")
    assert "[REDACTED_CARD]" in out


def test_redacts_recursively_in_dict_and_list() -> None:
    payload = {
        "user": {"email": "fatima.zahra@example.ae", "phone": "+971 50 123 4567"},
        "notes": ["passport B98765432", "no PII here"],
    }
    out = redact_pii(payload)
    assert "[REDACTED_EMAIL]" in out["user"]["email"]
    assert "[REDACTED_PHONE]" in out["user"]["phone"]
    assert "[REDACTED_PASSPORT]" in out["notes"][0]
    assert out["notes"][1] == "no PII here"


def test_redaction_is_idempotent() -> None:
    once = redact_pii("Email: aisha.malik@example.com")
    twice = redact_pii(once)
    assert once == twice


def test_redaction_preserves_non_string_types() -> None:
    out = redact_pii({"count": 5, "ratio": 0.42, "ok": True})
    assert out == {"count": 5, "ratio": 0.42, "ok": True}


# ---------------------------------------------------------------------------
# Tracer — emit + persist + read back
# ---------------------------------------------------------------------------


def test_tracer_writes_jsonl_and_reads_back(tmp_path: Path) -> None:
    tracer = Tracer(turn_id="t-001", trace_dir=tmp_path, redact=True)

    tracer.emit(
        node="router",
        prompt_id="router.v0",
        prompt_hash="abc123def456",
        latency_ms=120.5,
        tokens_in=50,
        tokens_out=12,
        cost_usd=0.000018,
        output={"intent": "flight_search", "rationale": "user mentioned Tokyo and August"},
    )
    tracer.emit(
        node="extractor",
        prompt_id="extractor.v1",
        prompt_hash="aaaa1111bbbb",
        latency_ms=410.0,
        tokens_in=850,
        tokens_out=220,
        cost_usd=0.00026,
        output={"flight_query": {"origin": "DXB", "destination": "NRT"}},
    )

    assert len(tracer.events) == 2
    assert tracer.events[0].node == "router"

    # On-disk roundtrip
    persisted = read_trace(tracer._path)
    assert len(persisted) == 2
    assert persisted[0].turn_id == "t-001"
    assert persisted[0].prompt_id == "router.v0"
    assert persisted[1].output["flight_query"]["origin"] == "DXB"


def test_tracer_redacts_pii_on_write(tmp_path: Path) -> None:
    tracer = Tracer(turn_id="t-002", trace_dir=tmp_path, redact=True)
    tracer.emit(
        node="extractor",
        latency_ms=10.0,
        output={"user_msg": "my passport B12345678 expires soon, email me at omar.farooq@example.com"},
    )
    persisted = read_trace(tracer._path)
    msg = persisted[0].output["user_msg"]
    assert "B12345678" not in msg
    assert "omar.farooq@example.com" not in msg
    assert "[REDACTED_PASSPORT]" in msg
    assert "[REDACTED_EMAIL]" in msg


def test_tracer_summary_aggregates_correctly(tmp_path: Path) -> None:
    tracer = Tracer(turn_id="t-003", trace_dir=tmp_path, redact=False)
    tracer.emit(node="router", latency_ms=100, tokens_in=50, tokens_out=10, cost_usd=0.0001)
    tracer.emit(node="extractor", latency_ms=200, tokens_in=500, tokens_out=100, cost_usd=0.0005)
    s = tracer.summary()
    assert s["node_count"] == 2
    assert s["total_latency_ms"] == 300.0
    assert s["total_tokens"] == 660
    assert s["total_cost_usd"] == 0.0006


def test_tracer_creates_dated_subdirectory(tmp_path: Path) -> None:
    from datetime import date as _date  # local import to keep top of file lean

    tracer = Tracer(turn_id="t-004", trace_dir=tmp_path)
    tracer.emit(node="router", latency_ms=1.0)
    expected = tmp_path / _date.today().isoformat() / "t-004.jsonl"
    assert expected.exists()
