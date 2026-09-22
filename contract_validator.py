#!/usr/bin/env python3
"""Validate the machine-readable pipeline contract and master JSON artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SPEC_PATH = Path(__file__).with_name("pipeline_spec.json")
MASTER_REQUIRED_FIELDS = (
    "schema_version",
    "artifact_type",
    "pool_id",
    "input",
    "processing",
    "artifacts",
    "quality_summary",
)
STAGE_RECORD_SECTIONS = ("cache", "input", "evaluation", "outputs", "quality", "handoff")
STAGE_SECTION_FIELDS = {
    "cache": ("key", "parameters", "reused", "source_stage_run"),
    "input": ("artifact_refs", "required_information", "fingerprint"),
    "evaluation": ("method", "rules", "decisions"),
    "outputs": ("artifact_refs", "records", "fingerprint"),
    "quality": ("confidence", "review_required", "errors", "warnings"),
    "handoff": ("accepted_refs", "pending_refs", "rejected_refs", "reason"),
}
STAGE_CONTRACT_FIELDS = (
    "id",
    "name",
    "input",
    "requires",
    "decisions",
    "outputs",
    "failure_modes",
    "handoff",
    "cache_key",
)
STAGE_LIST_FIELDS = ("input", "requires", "decisions", "outputs", "failure_modes", "cache_key")


def _is_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def _missing(value: Any, fields: tuple[str, ...], path: str) -> list[str]:
    if not _is_mapping(value):
        return [f"{path}: expected an object"]
    return [f"{path}.{field}: missing required field" for field in fields if field not in value]


def validate_spec(spec: Any) -> list[str]:
    errors = _missing(spec, ("schema_version", "artifact_type", "status_values", "status_meanings", "required_stage_record_fields", "stages"), "spec")
    if errors:
        return errors

    statuses = spec["status_values"]
    if not isinstance(statuses, list) or not statuses or not all(isinstance(item, str) for item in statuses):
        errors.append("spec.status_values: expected a non-empty list of strings")
    elif len(statuses) != len(set(statuses)):
        errors.append("spec.status_values: contains duplicates")

    if not isinstance(spec["status_meanings"], dict):
        errors.append("spec.status_meanings: expected an object")
    else:
        missing_meanings = set(statuses) - set(spec["status_meanings"])
        errors.extend(f"spec.status_meanings.{status}: missing status meaning" for status in sorted(missing_meanings))

    required_fields = spec["required_stage_record_fields"]
    if not isinstance(required_fields, list) or not all(isinstance(item, str) for item in required_fields):
        errors.append("spec.required_stage_record_fields: expected a list of strings")

    stages = spec["stages"]
    if not isinstance(stages, list) or not stages:
        return errors + ["spec.stages: expected a non-empty list"]

    stage_ids: list[str] = []
    for index, stage in enumerate(stages):
        path = f"spec.stages[{index}]"
        errors.extend(_missing(stage, STAGE_CONTRACT_FIELDS, path))
        if not _is_mapping(stage):
            continue
        stage_id = stage.get("id")
        if isinstance(stage_id, str):
            stage_ids.append(stage_id)
        else:
            errors.append(f"{path}.id: expected a string")
        for field in STAGE_LIST_FIELDS:
            if field in stage and not isinstance(stage[field], list):
                errors.append(f"{path}.{field}: expected a list")
        handoff = stage.get("handoff")
        if isinstance(handoff, str) and handoff != "final_artifacts" and handoff not in stage_ids and not any(item.get("id") == handoff for item in stages if _is_mapping(item)):
            errors.append(f"{path}.handoff: unknown stage {handoff!r}")

    if len(stage_ids) != len(set(stage_ids)):
        errors.append("spec.stages: contains duplicate stage IDs")
    return errors


def validate_master(master: Any, spec: dict[str, Any]) -> list[str]:
    errors = _missing(master, MASTER_REQUIRED_FIELDS, "master")
    if errors:
        return errors
    if master["schema_version"] != spec["schema_version"]:
        errors.append(f"master.schema_version: expected {spec['schema_version']!r}")
    if master["artifact_type"] != spec["artifact_type"]:
        errors.append(f"master.artifact_type: expected {spec['artifact_type']!r}")
    if not isinstance(master["pool_id"], str) or not master["pool_id"]:
        errors.append("master.pool_id: expected a non-empty string")

    processing = master["processing"]
    errors.extend(_missing(processing, ("stage_order", "stages"), "master.processing"))
    if not _is_mapping(processing):
        return errors

    expected_ids = [stage["id"] for stage in spec["stages"]]
    if processing.get("stage_order") != expected_ids:
        errors.append("master.processing.stage_order: does not match spec stage order")

    stage_records = processing.get("stages")
    if not isinstance(stage_records, list):
        return errors + ["master.processing.stages: expected a list"]
    if len(stage_records) != len(expected_ids):
        errors.append(f"master.processing.stages: expected {len(expected_ids)} records")

    for index, record in enumerate(stage_records):
        path = f"master.processing.stages[{index}]"
        required = tuple(spec["required_stage_record_fields"])
        errors.extend(_missing(record, required, path))
        if not _is_mapping(record):
            continue
        if index < len(expected_ids) and record.get("stage_id") != expected_ids[index]:
            errors.append(f"{path}.stage_id: does not match spec stage order")
        if record.get("status") not in spec["status_values"]:
            errors.append(f"{path}.status: unsupported status {record.get('status')!r}")
        for section in STAGE_RECORD_SECTIONS:
            errors.extend(_missing(record.get(section), STAGE_SECTION_FIELDS[section], f"{path}.{section}"))
    return errors


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_files(spec_path: Path, master_path: Path | None = None) -> list[str]:
    try:
        spec = load_json(spec_path)
    except (OSError, json.JSONDecodeError) as error:
        return [f"{spec_path}: cannot load JSON: {error}"]
    errors = validate_spec(spec)
    if master_path is None or errors:
        return errors
    try:
        master = load_json(master_path)
    except (OSError, json.JSONDecodeError) as error:
        return [f"{master_path}: cannot load JSON: {error}"]
    return errors + validate_master(master, spec)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--master", type=Path)
    args = parser.parse_args()
    errors = validate_files(args.spec, args.master)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("contract_valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())