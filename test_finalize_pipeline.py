from __future__ import annotations

import json
from pathlib import Path

from finalize_pipeline import require_llm_completion


ROOT = Path(__file__).parent


def test_finalization_requires_completed_llm_review() -> None:
    spec = json.loads((ROOT / "pipeline_spec.json").read_text(encoding="utf-8"))
    masters = []
    for pool_id in ("original", "children"):
        stages = [
            {
                "stage_id": stage["id"],
                "status": "pending",
            }
            for stage in spec["stages"]
        ]
        stages[8]["status"] = "pending"
        master = {"pool_id": pool_id, "processing": {"stages": stages}}
        masters.append(master)

    errors = require_llm_completion(masters)

    assert errors == [
        "original: llm_review status is 'pending'",
        "children: llm_review status is 'pending'",
    ]