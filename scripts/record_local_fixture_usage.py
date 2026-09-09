"""Write a local-only, explicitly unmetered Codex fixture usage ledger.

This utility records test provenance, not product-provider usage.  Codex does
not expose per-test token counters to this repository, so token and actual-cost
fields deliberately remain null rather than being estimated as real spend.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


PRICING_REFERENCE_URL = "https://developers.openai.com/api/docs/models/gpt-5.6-luna"
FORMULA = (
    "hypothetical_api_cost_usd = input_tokens / 1_000_000 * input_usd_per_1m "
    "+ output_tokens / 1_000_000 * output_usd_per_1m; do not apply when usage_status=unmetered"
)


def build_ledger() -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usage_status": "unmetered",
        "scope": "local Codex-authored test fixture and historical task dispatch metadata",
        "disclaimer": (
            "This ledger is not an OpenAI API invoice and does not claim actual API or Codex consumption. "
            "No per-run input/output token telemetry is available to the repository."
        ),
        "pricing_reference": {
            "url": PRICING_REFERENCE_URL,
            "observed_luna_text_usd_per_1m": {"input": 0.20, "cached_input": 0.02, "output": 1.20},
            "formula": FORMULA,
        },
        "runs": [
            {
                "run_id": "local-codex-fixture-smoke",
                "kind": "test_fixture",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
                "token_budget_total": 10_000_000,
                "input_tokens": None,
                "output_tokens": None,
                "actual_cost_usd": None,
                "usage_status": "unmetered",
                "notes": "Local deterministic planner/embedding fixture only; no external model or provider call.",
            },
            {
                "run_id": "historical-luna-dispatch",
                "kind": "historical_task_dispatch",
                "model": "gpt-5.6-luna",
                "reasoning_effort": None,
                "token_budget_total": None,
                "input_tokens": None,
                "output_tokens": None,
                "actual_cost_usd": None,
                "usage_status": "unmetered",
                "notes": "Model role recorded in LOCAL_HANDOFF.md; runtime token telemetry unavailable.",
            },
            {
                "run_id": "historical-terra-dispatch",
                "kind": "historical_task_dispatch",
                "model": "gpt-5.6-terra",
                "reasoning_effort": None,
                "token_budget_total": None,
                "input_tokens": None,
                "output_tokens": None,
                "actual_cost_usd": None,
                "usage_status": "unmetered",
                "notes": "Model role recorded in LOCAL_HANDOFF.md; runtime token telemetry unavailable.",
            },
            {
                "run_id": "historical-sol-dispatch",
                "kind": "historical_task_dispatch",
                "model": "gpt-5.6-sol",
                "reasoning_effort": None,
                "token_budget_total": None,
                "input_tokens": None,
                "output_tokens": None,
                "actual_cost_usd": None,
                "usage_status": "unmetered",
                "notes": "Model role recorded in LOCAL_HANDOFF.md; runtime token telemetry unavailable.",
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Record unmetered local Codex fixture metadata.")
    parser.add_argument("--ledger", type=Path, default=Path("content-os-data") / "usage" / "model-cost-ledger.json")
    args = parser.parse_args()
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    args.ledger.write_text(json.dumps(build_ledger(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
