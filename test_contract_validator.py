from __future__ import annotations

import copy
import json
from pathlib import Path

from contract_validator import validate_master, validate_spec


ROOT = Path(__file__).parent


def _valid_master(spec: dict) -> dict:
    records = []
    for stage in spec["stages"]:
        records.append(
            {
                "stage_id": stage["id"],
                "stage_name": stage["name"],
                "implementation_version": "test",
                "status": "pending",
                "cache": {"key": "", "parameters": {}, "reused": False, "source_stage_run": None},
                "input": {"artifact_refs": [], "required_information": [], "fingerprint": ""},
                "evaluation": {"method": "test", "rules": [], "decisions": []},
                "outputs": {"artifact_refs": [], "records": [], "fingerprint": ""},
                "quality": {"confidence": None, "review_required": False, "errors": [], "warnings": []},
                "handoff": {"accepted_refs": [], "pending_refs": [], "rejected_refs": [], "reason": "not run"},
                "started_at_utc": None,
                "completed_at_utc": None,
            }
        )
    return {
        "schema_version": spec["schema_version"],
        "artifact_type": spec["artifact_type"],
        "pool_id": "test",
        "input": {},
        "processing": {"stage_order": [stage["id"] for stage in spec["stages"]], "stages": records},
        "artifacts": {"sources": [], "regions": [], "cards": [], "reviews": []},
        "quality_summary": {},
    }


def test_spec_and_pending_master_are_valid() -> None:
    spec = json.loads((ROOT / "pipeline_spec.json").read_text(encoding="utf-8"))
    assert validate_spec(spec) == []
    assert validate_master(_valid_master(spec), spec) == []


def test_master_rejects_unknown_status() -> None:
    spec = json.loads((ROOT / "pipeline_spec.json").read_text(encoding="utf-8"))
    master = _valid_master(spec)
    master["processing"]["stages"][0]["status"] = "needs_review"
    errors = validate_master(master, spec)
    assert any("unsupported status" in error for error in errors)


def test_master_rejects_wrong_stage_order() -> None:
    spec = json.loads((ROOT / "pipeline_spec.json").read_text(encoding="utf-8"))
    master = copy.deepcopy(_valid_master(spec))
    master["processing"]["stages"][0].pop("handoff")
    errors = validate_master(master, spec)
    assert "master.processing.stages[0].handoff: missing required field" in errors