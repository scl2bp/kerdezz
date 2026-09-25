from __future__ import annotations

import json
from pathlib import Path

from contract_validator import validate_master
from finalize_pipeline import finalize
from test_contract_validator import _valid_master


ROOT = Path(__file__).parent


def test_finalize_publishes_validation_and_report_artifacts(tmp_path: Path) -> None:
    spec = json.loads((ROOT / "pipeline_spec.json").read_text(encoding="utf-8"))
    (tmp_path / "pipeline_spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    for pool_id in ("original", "children"):
        master = _valid_master(spec)
        master["pool_id"] = pool_id
        master["processing"]["stages"][8]["status"] = "available"
        path = tmp_path / "pipeline" / pool_id / "processing_master.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(master, ensure_ascii=False), encoding="utf-8")

    report_path = finalize(tmp_path, require_llm=True)

    assert report_path == tmp_path / "processing_report.md"
    assert report_path.is_file()
    for pool_id in ("original", "children"):
        master_path = tmp_path / "pipeline" / pool_id / "processing_master.json"
        master = json.loads(master_path.read_text(encoding="utf-8"))
        assert validate_master(master, spec) == []
        stages = {stage["stage_id"]: stage for stage in master["processing"]["stages"]}
        assert stages["contract_validation"]["status"] == "available"
        assert stages["processing_report"]["status"] == "available"
        assert stages["processing_report"]["outputs"]["artifact_refs"][0]["sha256"]
