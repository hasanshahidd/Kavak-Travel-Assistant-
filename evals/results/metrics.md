# Eval results

_Last run: 2026-05-02T11:09:05.065947+00:00 · mode: **mock**_

### Golden

- **Pass rate:** 100% (13/13)
- **Total latency:** 207 ms
- **Total cost (USD):** $0.005763

**By category:**

| Category | Passed | Total | Rate |
|---|---|---|---|
| extraction | 3 | 3 | 100% |
| multi_turn | 2 | 2 | 100% |
| rag | 3 | 3 | 100% |
| refusal | 1 | 1 | 100% |
| routing | 4 | 4 | 100% |

### Adversarial

- **Pass rate:** 100% (10/10)
- **Total latency:** 115 ms
- **Total cost (USD):** $0.005656

**By category:**

| Category | Passed | Total | Rate |
|---|---|---|---|
| empty_emoji | 1 | 1 | 100% |
| gibberish | 1 | 1 | 100% |
| hallucination_bait | 1 | 1 | 100% |
| hostile_tone | 1 | 1 | 100% |
| mixed_language | 1 | 1 | 100% |
| negation_trap | 1 | 1 | 100% |
| out_of_scope | 1 | 1 | 100% |
| pii | 1 | 1 | 100% |
| prompt_injection | 1 | 1 | 100% |
| prompt_injection_subtle | 1 | 1 | 100% |

> **Mode note.** Mock mode doesn't measure RAG or LLM-judgement quality -
> it exercises the wiring deterministically. Run `python -m evals.run_eval --real`
> with `OPENAI_API_KEY` set for measurements that reflect prompt quality.
