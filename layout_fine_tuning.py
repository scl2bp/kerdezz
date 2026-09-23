"""Validate and refine classified region candidates without extracting images."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image, ImageFilter

IMPLEMENTATION_VERSION = "layout-fine-tuning-v2"
GEOMETRY_RULES_VERSION = "normalized-bounds-overlap-local-frame-v1"
FRAME_REFINEMENT_VERSION = "local-frame-edge-run-v1"
CONSTANT_CARD_GEOMETRY_VERSION = "constant-card-content-pitch-v1"


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


def _content_extent(
    image: Image.Image,
    left: int,
    top: int,
    right: int,
    bottom: int,
    axis: str,
) -> tuple[int, int] | None:
    """Find the repeated ink band inside one coarse cell.

    Frame edges are mostly one-pixel-wide lines. Content occupies a wider
    contiguous band across the cell, so selecting the strongest wide run avoids
    treating a projection peak or frame edge as a card boundary.
    """
    gray = image.convert("L")
    width = max(1, right - left)
    height = max(1, bottom - top)
    inset_x = max(2, round(width * 0.03))
    inset_y = max(2, round(height * 0.04))
    if axis == "x":
        start, end = left + inset_x, right - inset_x
        profile = [
            sum(gray.getpixel((column, row)) < 180 for row in range(top + inset_y, bottom - inset_y))
            for column in range(start, end)
        ]
        minimum_run = max(8, round(width * 0.08))
    else:
        start, end = top + inset_y, bottom - inset_y
        profile = [
            sum(gray.getpixel((column, row)) < 180 for column in range(left + inset_x, right - inset_x))
            for row in range(start, end)
        ]
        minimum_run = max(8, round(height * 0.08))
    if not profile:
        return None
    threshold = max(2, round((bottom - top if axis == "x" else right - left) * 0.008))
    active = [value >= threshold for value in profile]
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for index, value in enumerate(active + [False]):
        if value and run_start is None:
            run_start = index
        elif not value and run_start is not None:
            runs.append((run_start, index))
            run_start = None
    merged: list[tuple[int, int]] = []
    merge_gap = max(4, round(len(profile) * 0.015))
    for run in runs:
        if merged and run[0] - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], run[1])
        else:
            merged.append(run)
    candidates = [run for run in merged if run[1] - run[0] >= minimum_run]
    if not candidates:
        return None
    selected = max(candidates, key=lambda run: (sum(profile[run[0] : run[1]]), run[1] - run[0]))
    return start + selected[0], start + selected[1] - 1


def _grid_boundaries(candidates: list[dict[str, Any]]) -> tuple[list[float], list[float]] | None:
    x_values = sorted({float(candidate["coordinates"][name]) for candidate in candidates for name in ("left", "right")})
    y_values = sorted({float(candidate["coordinates"][name]) for candidate in candidates for name in ("top", "bottom")})
    if len(x_values) < 3 or len(y_values) < 3:
        return None
    if any(abs(x_values[index + 1] - x_values[index]) < 0.08 for index in range(len(x_values) - 1)):
        return None
    if any(abs(y_values[index + 1] - y_values[index]) < 0.08 for index in range(len(y_values) - 1)):
        return None
    return x_values, y_values


def _frame_candidates(image: Image.Image, axis: str, expected: float) -> list[tuple[int, float]]:
    edge = image.convert("L").filter(ImageFilter.FIND_EDGES)
    width, height = image.size
    length = width if axis == "x" else height
    coordinate_start = max(1, round((expected - 0.13) * length))
    coordinate_end = min(length - 2, round((expected + 0.13) * length))
    scores: list[tuple[int, float]] = []
    for coordinate in range(coordinate_start, coordinate_end + 1):
        if axis == "x":
            values = [edge.getpixel((coordinate, row)) for row in range(round(height * 0.03), round(height * 0.97))]
        else:
            values = [edge.getpixel((column, coordinate)) for column in range(round(width * 0.03), round(width * 0.97))]
        longest = 0
        current = 0
        for value in values + [0]:
            current = current + 1 if value >= 15 else 0
            longest = max(longest, current)
        scores.append((coordinate, float(longest)))
    local_maxima = [
        item
        for index, item in enumerate(scores)
        if item[1] >= (scores[index - 1][1] if index else item[1])
        and item[1] >= (scores[index + 1][1] if index + 1 < len(scores) else item[1])
    ]
    minimum = max(12.0, length * 0.04)
    return sorted((item for item in local_maxima if item[1] >= minimum), key=lambda item: item[1], reverse=True)[:20]


def _constant_pitch_boundaries(image: Image.Image, axis: str, count: int) -> list[int] | None:
    length = image.width if axis == "x" else image.height
    if count != 3:
        return None
    first = _frame_candidates(image, axis, 1 / 3)
    second = _frame_candidates(image, axis, 2 / 3)
    if not first or not second:
        return None
    best: tuple[float, list[int]] | None = None
    for left, left_score in first:
        for right, right_score in second:
            widths = [left, right - left, length - right]
            if min(widths) < length * 0.2:
                continue
            spread = (max(widths) - min(widths)) / max(1.0, median(widths))
            support = (left_score + right_score) / max(1.0, length)
            score = spread - min(0.12, support * 0.08)
            if best is None or score < best[0]:
                best = (score, [0, left, right, length])
    return best[1] if best is not None else None


def estimate_constant_card_geometry(image: Image.Image, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Estimate constant card bounds from content bands and their gutters."""
    boundaries = _grid_boundaries(candidates)
    if boundaries is None:
        return None
    x_normalized, y_normalized = boundaries
    width, height = image.size
    x_pixels = [round(value * width) for value in x_normalized]
    y_pixels = [round(value * height) for value in y_normalized]
    x_bands: list[list[tuple[int, int]]] = [[] for _ in range(len(x_pixels) - 1)]
    y_bands: list[list[tuple[int, int]]] = [[] for _ in range(len(y_pixels) - 1)]
    for row in range(len(y_pixels) - 1):
        for column in range(len(x_pixels) - 1):
            x_band = _content_extent(image, x_pixels[column], y_pixels[row], x_pixels[column + 1], y_pixels[row + 1], "x")
            y_band = _content_extent(image, x_pixels[column], y_pixels[row], x_pixels[column + 1], y_pixels[row + 1], "y")
            if x_band is None or y_band is None:
                return None
            x_bands[column].append(x_band)
            y_bands[row].append(y_band)
    content_x = [(round(median([band[0] for band in bands])), round(median([band[1] for band in bands]))) for bands in x_bands]
    content_y = [(round(median([band[0] for band in bands])), round(median([band[1] for band in bands]))) for bands in y_bands]
    content_widths = [right - left + 1 for left, right in content_x]
    content_heights = [bottom - top + 1 for top, bottom in content_y]
    x_gaps = [content_x[index + 1][0] - content_x[index][1] - 1 for index in range(len(content_x) - 1)]
    y_gaps = [content_y[index + 1][0] - content_y[index][1] - 1 for index in range(len(content_y) - 1)]
    if min(x_gaps + y_gaps) <= 0:
        return None
    content_width = round(median(content_widths))
    content_height = round(median(content_heights))
    x_gap = round(median(x_gaps))
    y_gap = round(median(y_gaps))
    card_width = content_width + x_gap
    card_height = content_height + y_gap
    content_uniform = (
        max(content_widths) - min(content_widths) <= max(30, round(content_width * 0.25))
        and max(content_heights) - min(content_heights) <= max(40, round(content_height * 0.25))
    )
    x_centres = [(left + right) / 2 for left, right in content_x]
    y_centres = [(top + bottom) / 2 for top, bottom in content_y]
    x_card_boundaries = [round(x_centres[0] - card_width / 2)]
    x_card_boundaries.extend(round(center - card_width / 2) for center in x_centres[1:])
    x_card_boundaries.append(round(x_centres[-1] + card_width / 2))
    y_card_boundaries = [round(y_centres[0] - card_height / 2)]
    y_card_boundaries.extend(round(center - card_height / 2) for center in y_centres[1:])
    y_card_boundaries.append(round(y_centres[-1] + card_height / 2))
    bounds_fit_source = (
        x_card_boundaries[0] >= 0
        and x_card_boundaries[-1] <= width
        and y_card_boundaries[0] >= 0
        and y_card_boundaries[-1] <= height
    )
    if not content_uniform or not bounds_fit_source:
        frame_x = _constant_pitch_boundaries(image, "x", len(x_pixels) - 1)
        frame_y = _constant_pitch_boundaries(image, "y", len(y_pixels) - 1)
        if frame_x is None or frame_y is None:
            return None
        return {
            "method": CONSTANT_CARD_GEOMETRY_VERSION,
            "content_columns": [{"left": left, "right": right, "width": right - left + 1} for left, right in content_x],
            "content_rows": [{"top": top, "bottom": bottom, "height": bottom - top + 1} for top, bottom in content_y],
            "content_width": content_width,
            "content_height": content_height,
            "column_distance": round(median([frame_x[index + 1] - frame_x[index] for index in range(2)])),
            "row_distance": round(median([frame_y[index + 1] - frame_y[index] for index in range(2)])),
            "b_horizontal": round(median([frame_x[index + 1] - frame_x[index] for index in range(2)]) / 2, 3),
            "b_vertical": round(median([frame_y[index + 1] - frame_y[index] for index in range(2)]) / 2, 3),
            "card_width": round(median([frame_x[index + 1] - frame_x[index] for index in range(2)])),
            "card_height": round(median([frame_y[index + 1] - frame_y[index] for index in range(2)])),
            "outer_border": {"left": frame_x[0], "right": width - frame_x[-1], "top": frame_y[0], "bottom": height - frame_y[-1]},
            "pixel_boundaries": {"x": frame_x, "y": frame_y},
            "coordinate_system": "source pixels",
            "fallback_reason": "content height varies by text density; frame support and constant pitch selected",
        }
    return {
        "method": CONSTANT_CARD_GEOMETRY_VERSION,
        "content_columns": [{"left": left, "right": right, "width": right - left + 1} for left, right in content_x],
        "content_rows": [{"top": top, "bottom": bottom, "height": bottom - top + 1} for top, bottom in content_y],
        "content_width": content_width,
        "content_height": content_height,
        "column_distance": x_gap,
        "row_distance": y_gap,
        "b_horizontal": round(x_gap / 2, 3),
        "b_vertical": round(y_gap / 2, 3),
        "card_width": card_width,
        "card_height": card_height,
        "outer_border": {"left": x_card_boundaries[0], "right": width - x_card_boundaries[-1], "top": y_card_boundaries[0], "bottom": height - y_card_boundaries[-1]},
        "pixel_boundaries": {"x": x_card_boundaries, "y": y_card_boundaries},
        "coordinate_system": "source pixels",
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
    constant_geometry: dict[str, Any] | None = None
    if root is not None and layout_pattern.get("family") in {"grid", "composite_regions"}:
        source_path = root / str(source.get("path", ""))
        if source_path.is_file():
            with Image.open(source_path) as source_image:
                image = source_image.convert("RGB").copy()
            if layout_pattern.get("family") == "grid":
                constant_geometry = estimate_constant_card_geometry(image, candidates)
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
        if constant_geometry is not None:
            candidate_x = float(coordinates["left"])
            candidate_y = float(coordinates["top"])
            x_index = min(range(len(constant_geometry["pixel_boundaries"]["x"]) - 1), key=lambda index: abs(x_pixels := constant_geometry["pixel_boundaries"]["x"][index] / int(source["width"]) - candidate_x))
            y_index = min(range(len(constant_geometry["pixel_boundaries"]["y"]) - 1), key=lambda index: abs(y_pixels := constant_geometry["pixel_boundaries"]["y"][index] / int(source["height"]) - candidate_y))
            x_bounds = constant_geometry["pixel_boundaries"]["x"]
            y_bounds = constant_geometry["pixel_boundaries"]["y"]
            refined_coordinates = {
                "left": round(x_bounds[x_index] / int(source["width"]), 6),
                "top": round(y_bounds[y_index] / int(source["height"]), 6),
                "right": round(x_bounds[x_index + 1] / int(source["width"]), 6),
                "bottom": round(y_bounds[y_index + 1] / int(source["height"]), 6),
            }
            refinement_evidence = {"constant_card_geometry": constant_geometry}
            refinement_confidence = 0.9
        elif image is not None and candidate.get("layout_role") in {"dominant_vertical_grid", "secondary_horizontal_pair"}:
            refined_coordinates, refinement_evidence, refinement_confidence = _refine_frame_coordinates(coordinates, image)
        proposal["original_coordinates"] = coordinates
        proposal["refined_coordinates"] = refined_coordinates
        proposal["coordinates"] = refined_coordinates
        proposal["refinement_method"] = constant_geometry["method"] if constant_geometry is not None else (FRAME_REFINEMENT_VERSION if image is not None else "none")
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