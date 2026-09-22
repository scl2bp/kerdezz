from __future__ import annotations

import json
from pathlib import Path

from generate_processing_report import render_report


ROOT = Path(__file__).parent


def test_report_distinguishes_validated_contract_from_unexecuted_pipeline() -> None:
    spec = json.loads((ROOT / "pipeline_spec.json").read_text(encoding="utf-8"))
    report = render_report(spec, [], [], [])
    assert "Contract validation: **PASS**" in report
    assert "Processing masters found: **0** of 2 expected pools" in report
    assert "Pipeline stage records attempted: **0**" in report
    assert "| Unexecuted planned stages | 9 |" in report
    assert "archive processing and downstream stages are not yet executed" in report
    assert "No `pipeline/<pool>/processing_master.json` exists yet." in report
    assert "| ZIP archive | `zip_archive` | pending (not executed) | 0 members, 0 selected |" in report


def test_report_aggregates_master_kpis() -> None:
    spec = json.loads((ROOT / "pipeline_spec.json").read_text(encoding="utf-8"))
    master = {
        "pool_id": "original",
        "artifacts": {
            "sources": [{"id": "s1"}],
            "regions": [{"id": "r1"}],
            "cards": [{"id": "c1"}],
            "reviews": [],
        },
        "processing": {"stages": [{"stage_id": "zip_archive", "status": "available"}]},
        "quality_summary": {},
    }
    report = render_report(
        spec,
        [master],
        [ROOT / "pipeline/original/processing_master.json"],
        [],
    )
    assert "Processing masters found: **1** of 2 expected pools" in report
    assert "Pipeline stage records attempted: **1**" in report
    assert "| Source artifacts | 1 |" in report
    assert "| Card artifacts | 1 |" in report
    assert "| Unexecuted planned stages | 8 |" in report
    assert "| ZIP archive | `zip_archive` | available: 1 | 0 members, 0 selected |" in report
