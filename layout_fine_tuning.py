"""Validate and refine classified region candidates without extracting images."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter

IMPLEMENTATION_VERSION = "layout-fine-tuning-v2"
GEOMETRY_RULES_VERSION = "normalized-bounds-overlap-local-frame-v1"
FRAME_REFINEMENT_VERSION = "local-frame-edge-run-v1"


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


def _longest_edge_run(values: list[int], threshold: int = 18) -> int:
    longest = 0
    current = 0
    for value in values:
        current = current + 1 if value >= threshold else 0
        longest = max(longest, current)
    return longest


def _boundary_evidence(
    image: Image.Image,
    coordinates: dict[str, float],
    side: str,
) -> tuple[float, float, float]:
    width, height = image.size
    edge = image.convert("L").filter(ImageFilter.FIND_EDGES)
    if side in {"top", "bottom"}:
        expected = coordinates[side]
        start = max(0, round(coordinates["left"] * width))
        end = min(width, round(coordinates["right"] * width))
        centre = round(expected * height)
        radius = max(24, round(height * 0.08))
        candidates = range(max(0, centre - radius), min(height, centre + radius + 1))
        support = []
        for row in candidates:
            values = [edge.getpixel((column, row)) for column in range(start, end)]
            support.append((row, _longest_edge_run(values)))
        selected, run = max(support, key=lambda value: value[1], default=(centre, 0))
        span = max(1, end - start)
        return expected, round(selected / height, 6), round(run / span, 6)
    expected = coordinates[side]
    start = max(0, round(coordinates["top"] * height))
    end = min(height, round(coordinates["bottom"] * height))
    centre = round(expected * width)
    radius = max(24, round(width * 0.08))
    candidates = range(max(0, centre - radius), min(width, centre + radius + 1))
    support = []
    for column in candidates:
        values = [edge.getpixel((column, row)) for row in range(start, end)]
        support.append((column, _longest_edge_run(values)))
    selected, run = max(support, key=lambda value: value[1], default=(centre, 0))
    span = max(1, end - start)
    return expected, round(selected / width, 6), round(run / span, 6)


def _refine_frame_coordinates(
    coordinates: dict[str, float],
    image: Image.Image,
) -> tuple[dict[str, float], dict[str, Any], float]:
    refined = dict(coordinates)
    evidence: dict[str, dict[str, float | bool]] = {}
    confidence_values: list[float] = []
    for side in ("left", "top", "right", "bottom"):
        if coordinates[side] in {0.0, 1.0}:
            evidence[side] = {"expected": coordinates[side], "selected": coordinates[side], "support_ratio": 1.0, "refined": False}
            continue
        expected, selected, support_ratio = _boundary_evidence(image, coordinates, side)
        accepted = support_ratio >= 0.35
        if accepted:
            refined[side] = selected
            confidence_values.append(min(1.0, support_ratio / 0.7))
        evidence[side] = {"expected": expected, "selected": selected, "support_ratio": support_ratio, "refined": accepted}
    if refined["right"] <= refined["left"] or refined["bottom"] <= refined["top"]:
        return coordinates, evidence, 0.0
    return refined, evidence, round(sum(confidence_values) / max(1, len(confidence_values)), 6)


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


def refine_source_layout(source: dict[str, Any], decision: dict[str, Any], root: Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    route = decision.get("routing_class")
    if route in {"unknown", "non_card"}:
        return [], []
    candidates = decision.get("observation_profile", {}).get("region_candidates", [])
    if route == "individual_card":
        candidates = [_individual_candidate(source)]
    proposals: list[dict[str, Any]] = []
    errors: list[str] = []
    accepted_coordinates: list[dict[str, float]] = []
    image: Image.Image | None = None
    layout_pattern = decision.get("observation_profile", {}).get("layout_pattern", {})
    if root is not None and layout_pattern.get("family") == "composite_regions":
        source_path = root / str(source.get("path", ""))
        if source_path.is_file():
            with Image.open(source_path) as source_image:
                image = source_image.convert("RGB").copy()
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
        refined_coordinates = coordinates
        refinement_evidence: dict[str, Any] = {}
        refinement_confidence = 0.0
        if image is not None and candidate.get("layout_role") in {"dominant_vertical_grid", "secondary_horizontal_pair"}:
            refined_coordinates, refinement_evidence, refinement_confidence = _refine_frame_coordinates(coordinates, image)
        proposal["original_coordinates"] = coordinates
        proposal["refined_coordinates"] = refined_coordinates
        proposal["coordinates"] = refined_coordinates
        proposal["refinement_method"] = FRAME_REFINEMENT_VERSION if image is not None else "none"
        proposal["refinement_confidence"] = refinement_confidence
        proposal["boundary_evidence"] = refinement_evidence
        proposal["pixel_coordinates"] = _pixel_box(refined_coordinates, int(source["width"]), int(source["height"]))
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
        source_proposals, source_errors = refine_source_layout(source, decision, root)
        errors.extend(source_errors)
        for proposal in source_proposals:
            record = {
                "artifact_type": "layout_region_proposal",
                "implementation_version": IMPLEMENTATION_VERSION,
                "created_at_utc": utc_now(),
                "source": {"source_id": source["source_id"], "member_name": source["member_name"], "sha256": source["file_sha256"], "width": source["width"], "height": source["height"]},
                "classification_decision_ref": decision.get("artifact_ref"),
                "proposal": proposal,
                "evaluation": {"method": "deterministic normalized geometry validation and local frame refinement", "rules": ["coordinates are normalized and in bounds", "accepted proposals do not overlap unexpectedly", "pixel box is derived from source dimensions", "local frame refinement is accepted only for strong edge-run evidence"]},
                "quality": {"confidence": proposal.get("confidence"), "review_required": False, "warnings": [], "errors": []},
            }
            proposal_key = object_hash({"source": source["file_sha256"], "coordinates": proposal["coordinates"], "transform": proposal.get("transform"), "rules": GEOMETRY_RULES_VERSION, "refinement": proposal.get("refinement_method")})
            record["proposal_key"] = proposal_key
            path = proposal_dir / proposal["source_id"] / f"{proposal_key}.json"
            file_hash = atomic_json(path, record)
            record["artifact_ref"] = {"path": relative(path, root), "sha256": file_hash}
            proposal["artifact_ref"] = record["artifact_ref"]
            proposal["proposal_key"] = proposal_key
            proposals.append(proposal)
            artifacts.append(record["artifact_ref"])
    return proposals, artifacts, errors