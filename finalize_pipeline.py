#!/usr/bin/env python3
"""Validate one completed pool master and publish its processing report."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contract_validator import load_json, validate_files, validate_master
from generate_processing_report import render_report


ROOT = Path(__file__).parent
EXPECTED_POOLS = ("original", "children")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return sha256_file(path)


def stage_record(master: dict[str, Any], stage_id: str) -> dict[str, Any]:
    return next(record for record in master["processing"]["stages"] if record["stage_id"] == stage_id)


def require_llm_completion(masters: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for master in masters:
        record = stage_record(master, "llm_review")
        if record.get("status") not in {"available", "cached"}:
            errors.append(f"{master.get('pool_id', 'unknown')}: llm_review status is {record.get('status')!r}")
    return errors


def report_stage(master: dict[str, Any]) -> dict[str, Any]:
    return stage_record(master, "processing_report")


def artifact_reference_errors(master: dict[str, Any], root: Path) -> list[str]:
    errors: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            reference = value.get("path")
            if isinstance(reference, str) and reference:
                target = root / reference
                if not target.is_file():
                    errors.append(f"{path}.path: referenced artifact is missing: {reference}")
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(master, "master")
    return errors


def finalize(root: Path, require_llm: bool, pool_id: str) -> Path:
    spec_path = root / "pipeline_spec.json"
    spec = load_json(spec_path)
    if pool_id not in EXPECTED_POOLS:
        raise ValueError(f"unknown pool: {pool_id}")
    master_paths = [root / "pipeline" / pool_id / "processing_master.json"]
    missing = [str(path) for path in master_paths if not path.is_file()]
    if missing:
        raise ValueError("missing expected pool master(s): " + ", ".join(missing))
    masters = [load_json(path) for path in master_paths]

    errors: list[str] = []
    for path, master in zip(master_paths, masters):
        errors.extend(f"{path}: {error}" for error in validate_master(master, spec))
        errors.extend(f"{path}: {error}" for error in artifact_reference_errors(master, root))
    if require_llm:
        errors.extend(require_llm_completion(masters))
    if errors:
        raise ValueError("finalization blocked: " + "; ".join(errors))

    generated = utc_now()
    for path, master in zip(master_paths, masters):
        validation_path = path.parent / "finalization" / "contract_validation.json"
        validation_document = {
            "artifact_type": "pipeline_contract_validation",
            "generated_at_utc": generated,
            "pool_id": master["pool_id"],
            "spec_path": str(spec_path.relative_to(root)),
            "spec_sha256": sha256_file(spec_path),
            "master_path": str(path.relative_to(root)),
            "status": "valid",
            "errors": [],
        }
        validation_sha = write_json(validation_path, validation_document)
        record = stage_record(master, "contract_validation")
        record.update(
            {
                "status": "available",
                "started_at_utc": generated,
                "completed_at_utc": generated,
                "cache": {"key": object_hash({"master": sha256_file(path), "spec": validation_document["spec_sha256"]}), "parameters": {}, "reused": False, "source_stage_run": None},
                "input": {"artifact_refs": [{"path": str(path.relative_to(root)), "sha256": sha256_file(path)}, {"path": str(spec_path.relative_to(root)), "sha256": validation_document["spec_sha256"]}], "required_information": record["input"]["required_information"], "fingerprint": object_hash({"master": sha256_file(path), "spec": validation_document["spec_sha256"]})},
                "outputs": {"artifact_refs": [{"path": str(validation_path.relative_to(root)), "sha256": validation_sha}], "records": [validation_document], "fingerprint": validation_sha},
                "quality": {"confidence": 1.0, "review_required": False, "errors": [], "warnings": []},
                "handoff": {"accepted_refs": [master["pool_id"]], "pending_refs": [], "rejected_refs": [], "reason": "validated master is ready for pool reporting"},
            }
        )
        report_record = report_stage(master)
        report_record.update(
            {
                "status": "available",
                "started_at_utc": generated,
                "completed_at_utc": generated,
                "cache": {"key": object_hash({"master": str(path), "generator": "processing-report-v3", "pool_id": master["pool_id"]}), "parameters": {"pool_id": master["pool_id"]}, "reused": False, "source_stage_run": None},
                "input": {"artifact_refs": [{"path": str(path.relative_to(root))}], "required_information": report_record["input"]["required_information"], "fingerprint": object_hash(str(path))},
                "outputs": {"artifact_refs": [{"path": f"pipeline/{master['pool_id']}/processing_report.md"}], "records": [], "fingerprint": ""},
                "quality": {"confidence": 1.0, "review_required": False, "errors": [], "warnings": []},
                "handoff": {"accepted_refs": [f"pipeline/{master['pool_id']}/processing_report.md"], "pending_refs": [], "rejected_refs": [], "reason": "pool report is ready as a terminal artifact"},
            }
        )

    for path, master in zip(master_paths, masters):
        write_json(path, master)

    report_path = root / "pipeline" / pool_id / "processing_report.md"
    report_errors: list[str] = []
    for path in master_paths:
        report_errors.extend(f"{path}: {error}" for error in validate_files(spec_path, path))
    report = render_report(spec, masters, master_paths, report_errors)
    report_path.write_text(report, encoding="utf-8")
    report_sha = sha256_file(report_path)

    for path, master in zip(master_paths, masters):
        report_stage(master)["outputs"] = {
            "artifact_refs": [{"path": str(report_path.relative_to(root)), "sha256": report_sha}],
            "records": [{"pool_id": master["pool_id"], "master_path": str(path.relative_to(root))}],
            "fingerprint": report_sha,
        }
        write_json(path, master)

    final_errors: list[str] = []
    for path, master in zip(master_paths, masters):
        final_errors.extend(f"{path}: {error}" for error in validate_master(master, spec))
    if final_errors:
        raise ValueError("final masters failed validation: " + "; ".join(final_errors))
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--pool", choices=EXPECTED_POOLS, required=True)
    parser.add_argument("--require-llm", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    try:
        report_path = finalize(args.root.resolve(), args.require_llm, args.pool)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
