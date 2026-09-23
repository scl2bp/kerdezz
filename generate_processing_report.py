#!/usr/bin/env python3
"""Generate one concise Markdown report for the complete quiz-card pipeline."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contract_validator import validate_files


ROOT = Path(__file__).parent
DEFAULT_SPEC = ROOT / "pipeline_spec.json"
DEFAULT_OUTPUT = ROOT / "processing_report.md"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def discover_masters(root: Path) -> list[Path]:
    return sorted(root.glob("pipeline/*/processing_master.json"))


def records(master: dict[str, Any]) -> list[dict[str, Any]]:
    processing = master.get("processing", {})
    stage_records = processing.get("stages", []) if isinstance(processing, dict) else []
    return [record for record in stage_records if isinstance(record, dict)]


def count_items(master: dict[str, Any], key: str) -> int:
    artifacts = master.get("artifacts", {})
    values = artifacts.get(key, []) if isinstance(artifacts, dict) else []
    return len(values) if isinstance(values, list) else 0


def stage_scope(stage_id: str, masters: list[dict[str, Any]]) -> str:
    quality_values = [
        record.get("quality", {})
        for master in masters
        for record in records(master)
        if record.get("stage_id") == stage_id
    ]
    if stage_id == "zip_archive":
        members = sum(value.get("member_count", 0) for value in quality_values)
        selected = sum(value.get("selected_member_count", 0) for value in quality_values)
        return f"{members} members, {selected} selected"
    if stage_id == "source_files":
        return f"{sum(value.get('source_count', 0) for value in quality_values)} sources"
    if stage_id == "collection_images":
        return f"{sum(value.get('cell_count', 0) for value in quality_values)} source cells"
    if stage_id == "page_classification":
        source_count = sum(value.get("source_count", 0) for value in quality_values)
        feature_count = sum(value.get("feature_artifact_count", 0) for value in quality_values)
        return f"{source_count} sources, {feature_count} feature artifacts, {sum(count_items(master, 'regions') for master in masters)} accepted regions"
    if stage_id == "layout_fine_tuning":
        return f"{sum(value.get('proposal_count', 0) for value in quality_values)} proposals"
    if stage_id == "card_extraction":
        return f"{sum(value.get('card_count', 0) for value in quality_values)} cards"
    if stage_id == "orientation_estimation":
        return f"{sum(value.get('card_count', 0) for value in quality_values)} oriented cards"
    if stage_id == "ocr_extraction":
        return f"{sum(value.get('card_count', 0) for value in quality_values)} OCR records"
    return ""


def status_counts(masters: list[dict[str, Any]]) -> Counter[str]:
    return Counter(record.get("status", "missing") for master in masters for record in records(master))


def stage_rows(spec: dict[str, Any], masters: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for stage in spec["stages"]:
        stage_id = stage["id"]
        values = [
            record.get("status", "missing")
            for master in masters
            for record in records(master)
            if record.get("stage_id") == stage_id
        ]
        if not values:
            status = "pending (not executed)"
        else:
            counts = Counter(values)
            status = ", ".join(f"{name}: {counts[name]}" for name in sorted(counts))
        rows.append(f"| {stage['name']} | `{stage_id}` | {status} | {stage_scope(stage_id, masters)} |")
    return rows


def next_gate(spec: dict[str, Any], masters: list[dict[str, Any]]) -> str:
    if not masters:
        return "Run the archive/source phase for a bounded probe (`original`, first 3 members), then regenerate this report."
    for stage in spec["stages"]:
        stage_records = [
            record
            for master in masters
            for record in records(master)
            if record.get("stage_id") == stage["id"]
        ]
        if not stage_records or any(record.get("status") == "pending" for record in stage_records):
            return f"Implement and run the `{stage['id']}` phase for the selected pools, then regenerate this report."
    return "All defined stages have non-pending records; review the final quality and failure KPIs."


def render_report(
    spec: dict[str, Any],
    masters: list[dict[str, Any]],
    master_paths: list[Path],
    contract_errors: list[str],
) -> str:
    contract_status = "PASS" if not contract_errors else "FAIL"
    counts = status_counts(masters)
    source_count = sum(count_items(master, "sources") for master in masters)
    region_count = sum(count_items(master, "regions") for master in masters)
    card_count = sum(count_items(master, "cards") for master in masters)
    review_count = sum(count_items(master, "reviews") for master in masters)
    expected_stage_records = len(spec.get("stages", [])) * len(masters)
    absent_stage_records = max(0, expected_stage_records - sum(counts.values()))
    unexecuted_stage_records = counts["pending"] + absent_stage_records
    attempted_stage_records = sum(counts.values()) - counts["pending"]
    if not masters:
        unexecuted_stage_records = len(spec.get("stages", []))
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines = [
        "# Quiz Card Processing Report",
        "",
        f"Generated: `{generated}`",
        "",
        "## Executive outcome",
        "",
        f"- Contract validation: **{contract_status}**",
        f"- Processing masters found: **{len(masters)}** of 2 expected pools",
        f"- Pipeline stages defined: **{len(spec.get('stages', []))}**",
        f"- Pipeline stage records attempted: **{attempted_stage_records}**",
        (
            "- Current boundary: the contract layer is validated; archive processing and downstream stages are not yet executed."
            if not masters
            else "- Current boundary: status below reflects the available pool master artifacts."
        ),
        "",
        "## KPI summary",
        "",
        "| KPI | Value |",
        "|---|---:|",
        f"| Pools with master JSON | {len(masters)} |",
        f"| Source artifacts | {source_count} |",
        f"| Region proposals | {region_count} |",
        f"| Card artifacts | {card_count} |",
        f"| Review artifacts | {review_count} |",
        f"| Unexecuted planned stages | {unexecuted_stage_records} |",
        f"| Stage records present | {sum(counts.values())} |",
        f"| Available stages | {counts['available']} |",
        f"| Cached stages | {counts['cached']} |",
        f"| Pending stages | {counts['pending']} |",
        f"| Model-review pending stages | {counts['model_review_pending']} |",
        f"| Model-uncertain stages | {counts['model_uncertain']} |",
        f"| Rejected stages | {counts['rejected']} |",
        f"| Failed stages | {counts['failed']} |",
        "",
        "## Stage status",
        "",
        "| Phase | Stage ID | Status | Scope |",
        "|---|---|---|---|",
        *stage_rows(spec, masters),
        "",
        "## Pool coverage",
        "",
    ]
    if masters:
        lines.extend(
            [
                "| Pool | Master artifact | Sources | Regions | Cards | Reviews |",
                "|---|---|---:|---:|---:|---:|",
            ]
        )
        for path, master in zip(master_paths, masters):
            lines.append(
                f"| `{master.get('pool_id', path.parent.name)}` | `{path.relative_to(ROOT)}` | "
                f"{count_items(master, 'sources')} | {count_items(master, 'regions')} | "
                f"{count_items(master, 'cards')} | {count_items(master, 'reviews')} |"
            )
    else:
        lines.append(
            "No `pipeline/<pool>/processing_master.json` exists yet. "
            "This is expected before archive/source execution."
        )
    lines.extend(
        [
            "",
            "## Contract review",
            "",
            f"- Schema version: `{spec.get('schema_version', 'unknown')}`",
            f"- Artifact type: `{spec.get('artifact_type', 'unknown')}`",
            f"- Allowed statuses: {', '.join(f'`{value}`' for value in spec.get('status_values', []))}",
        ]
    )
    if contract_errors:
        lines.extend(["", "### Validation errors", "", *[f"- {error}" for error in contract_errors]])
    else:
        lines.append("- The specification and its status vocabulary are structurally valid.")
    lines.extend(
        [
            "",
            "## Next gate",
            "",
            next_gate(spec, masters) + " Do not interpret pending downstream stages as failures; they have not been executed yet.",
            "",
            "## Source artifacts",
            "",
            "- Specification: [pipeline_spec.json](pipeline_spec.json)",
            "- Implementation plan: [pipeline_implementation_plan.md](pipeline_implementation_plan.md)",
            "- Contract validator: [contract_validator.py](contract_validator.py)",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--master", type=Path, action="append", help="Master JSON path; repeat for multiple pools.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    spec = load_json(args.spec)
    master_paths = list(args.master) if args.master else discover_masters(ROOT)
    masters = [load_json(path) for path in master_paths]
    contract_errors = validate_files(args.spec)
    args.output.write_text(
        render_report(spec, masters, master_paths, contract_errors),
        encoding="utf-8",
    )
    print(args.output)
    return 0 if not contract_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
