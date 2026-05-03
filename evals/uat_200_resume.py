"""Resume UAT-200 from where it died — only run queries 171-200."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.graph.builder import build_agent, default_substrate  # noqa: E402
from evals.uat_200 import QUERIES, _run, _verdict  # noqa: E402

TRACE_DIR = ROOT / ".traces" / "uat-200"


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        from app.config import get_settings
        if not get_settings().openai_api_key:
            print("OPENAI_API_KEY not set", file=sys.stderr)
            return 1

    sub = default_substrate(self_critique=False)
    agent = build_agent(sub)

    # Skip first 170 (already traced); run 171-200
    remaining = QUERIES[170:]
    pass_count = 0
    rows = []
    for i, (label, query, exp_intent, exp_subcat) in enumerate(remaining, start=171):
        time.sleep(0.4)
        row = _run(query, agent, label)
        verdict = _verdict(row, exp_intent, exp_subcat)
        rows.append((label, query, exp_intent, exp_subcat, row, verdict))
        if verdict == "PASS":
            pass_count += 1
        cat = f"/{row['oos_cat']}" if row.get("oos_cat") else ""
        print(f"[{verdict:5}] {i:3}/200 {label:25} -> {row['intent']}{cat} {row['latency_ms']:.0f}ms", flush=True)

    print()
    print(f"# UAT-200 RESUME — last 30: {pass_count}/{len(remaining)} pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
