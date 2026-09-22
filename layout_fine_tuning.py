"""Validate and refine classified region candidates without extracting images."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IMPLEMENTATION_VERSION = "layout-fine-tuning-v1"
GEOMETRY_RULES_VERSION = "normalized-bounds-overlap-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def object_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def atomic_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return sha256_file(path)


def _coordinates(candidate: dict[str, Any]) -> dict[str, float] | None:
    value = candidate.get("coordinates")
    if not isinstance(value, dict):
        return None
    names = ("left", "top", "right", "bottom")
    if not all(isinstance(value.get(name), (int, float)) for name in names):
        return None
    coordinates = {name: float(value[name]) for name in names}
    if not all(0.0 <= coordinates[name] <= 1.0 for name in names):
        return None
    if coordinates["right"] <= coordinates["left"] or coordinates["bottom"] <= coordinates["top"]:
        return None
    return coordinates


def _overlap(left: dict[str, float], right: dict[str, float]) -> float:
    width = max(0.0, min(left["right"], right["right"]) - max(left["left"], right["left"]))
    height = max(0.0, min(left["bottom"], right["bottom"]) - max(left["top"], right["top"]))
    intersection = width * height
    left_area = (left["right"] - left["left"]) * (left["bottom"] - left["top"])
    right_area = (right["right"] - right["left"]) * (right["bottom"] - right["top"])
    return intersection / max(1e-12, min(left_area, right_area))


def _pixel_box(coordinates: dict[str, float], width: int, height: int) -> dict[str, int]:
    return {
        "left": round(coordinates["left"] * width),
        "top": round(coordinates["top"] * height),
        "right": round(coordinates["right"] * width),
        "bottom": round(coordinates["bottom"] * height),
    }


def _individual_candidate(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "region_id": "full_source",
        "role": "quiz_card_candidate",
        "layout_role": "individual_card_source",
        "group_id": "individual_card_source",
        "orientation": "unknown",
        "coordinates": {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0},
        "transform": {"rotation_degrees": 0, "rotation_candidates": [0, 90, -90, 180]},
        "producer": "deterministic_individual_source_geometry",
        "confidence": 0.9,
    }


def refine_source_layout(source: dict[str, Any], decision: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    route = decision.get("routing_class")
    if route in {"unknown", "non_card"}:
        return [], []
    candidates = decision.get("observation_profile", {}).get("region_candidates", [])
    if route == "individual_card":
        candidates = [_individual_candidate(source)]
    proposals: list[dict[str, Any]] = []
    errors: list[str] = []
    accepted_coordinates: list[dict[str, float]] = []
    for index, candidate in enumerate(candidates):
        coordinates = _coordinates(candidate)
        if coordinates is None:
            errors.append(f"{source['source_id']}:{candidate.get('region_id', index)}: invalid normalized coordinates")
            continue
        overlap = max((_overlap(coordinates, prior) for prior in accepted_coordinates), default=0.0)
        if overlap > 0.2:
            errors.append(f"{source['source_id']}:{candidate.get('region_id', index)}: unexpected overlap {overlap:.3f}")
            continue
        accepted_coordinates.append(coordinates)
        proposal = dict(candidate)
        proposal["coordinates"] = coordinates
        proposal["pixel_coordinates"] = _pixel_box(coordinates, int(source["width"]), int(source["height"]))
        proposal["source_id"] = source["source_id"]
        proposal["source_sha256"] = source["file_sha256"]
        proposal["classification_decision_ref"] = decision.get("artifact_ref")
        proposal["status"] = "accepted"
        proposal["geometry_rules_version"] = GEOMETRY_RULES_VERSION
        proposals.append(proposal)
    return proposals, errors


def fine_tune_layouts(
    sources: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    root: Path,
    pool_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    proposal_dir = pool_root / "regions"
    decisions_by_source = {decision["source_id"]: decision for decision in decisions}
    proposals: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    errors: list[str] = []
    for source in sources:
        decision = decisions_by_source.get(source["source_id"])
        if not decision:
            continue
        source_proposals, source_errors = refine_source_layout(source, decision)
        errors.extend(source_errors)
        for proposal in source_proposals:
            record = {
                "artifact_type": "layout_region_proposal",
                "implementation_version": IMPLEMENTATION_VERSION,
                "created_at_utc": utc_now(),
                "source": {"source_id": source["source_id"], "member_name": source["member_name"], "sha256": source["file_sha256"], "width": source["width"], "height": source["height"]},
                "classification_decision_ref": decision.get("artifact_ref"),
                "proposal": proposal,
                "evaluation": {"method": "deterministic normalized geometry validation", "rules": ["coordinates are normalized and in bounds", "accepted proposals do not overlap unexpectedly", "pixel box is derived from source dimensions"]},
                "quality": {"confidence": proposal.get("confidence"), "review_required": False, "warnings": [], "errors": []},
            }
            proposal_key = object_hash({"source": source["file_sha256"], "coordinates": proposal["coordinates"], "transform": proposal.get("transform"), "rules": GEOMETRY_RULES_VERSION})
            record["proposal_key"] = proposal_key
            path = proposal_dir / proposal["source_id"] / f"{proposal_key}.json"
            file_hash = atomic_json(path, record)
            record["artifact_ref"] = {"path": relative(path, root), "sha256": file_hash}
            proposal["artifact_ref"] = record["artifact_ref"]
            proposal["proposal_key"] = proposal_key
            proposals.append(proposal)
            artifacts.append(record["artifact_ref"])
    return proposals, artifacts, errors