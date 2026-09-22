"""Evidence-producing source and collection classification helpers.

The module deliberately separates image observations, collection/model evidence,
and routing fusion. Model output is retained as evidence and is never used as a
geometry decision without deterministic validation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image, ImageFilter, ImageOps


FEATURE_RULES_VERSION = "projection-features-v1"
CLASSIFICATION_RULES_VERSION = "observation-fusion-v1"
MODEL_PROMPT_VERSION = "collection-routing-evidence-v1"
IMPLEMENTATION_VERSION = "classification-v1"
ROUTING_CLASSES = ("card_collection", "individual_card", "non_card", "unknown")
SAMPLE_LONG_SIDE = 320


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)


def relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def atomic_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return sha256_file(path)


def _pixel_values(image: Image.Image) -> list[int]:
    getter = getattr(image, "get_flattened_data", image.getdata)
    return list(getter())


def _sample_image(path: Path) -> tuple[Image.Image, int, int]:
    with Image.open(path) as original:
        width, height = original.size
        image = ImageOps.grayscale(original.convert("RGB"))
        scale = min(1.0, SAMPLE_LONG_SIDE / max(width, height))
        if scale < 1:
            image = image.resize((max(2, round(width * scale)), max(2, round(height * scale))), Image.Resampling.LANCZOS)
        return image.copy(), width, height


def _axis_profile(image: Image.Image, axis: str) -> list[float]:
    width, height = image.size
    pixels = _pixel_values(image)
    if axis == "vertical":
        return [
            sum(abs(pixels[row * width + column] - pixels[row * width + column - 1]) for row in range(height)) / height
            for column in range(1, width)
        ]
    return [
        sum(abs(pixels[row * width + column] - pixels[(row - 1) * width + column]) for column in range(width)) / width
        for row in range(1, height)
    ]


def _ink_profile(image: Image.Image, axis: str) -> list[float]:
    width, height = image.size
    pixels = _pixel_values(image)
    if axis == "vertical":
        return [sum(255 - pixels[row * width + column] for row in range(height)) / height for column in range(width)]
    return [sum(255 - pixels[row * width + column] for column in range(width)) / width for row in range(height)]


def _smooth(values: list[float]) -> list[float]:
    if len(values) < 3:
        return values[:]
    return [values[0]] + [sum(values[index - 1 : index + 2]) / 3 for index in range(1, len(values) - 1)] + [values[-1]]


def _separator_peaks(values: list[float]) -> tuple[list[dict[str, float]], dict[str, float]]:
    if not values:
        return [], {"minimum": 0.0, "maximum": 0.0, "median": 0.0, "threshold": 0.0}
    smoothed = _smooth(values)
    centre = median(smoothed)
    deviations = [abs(value - centre) for value in smoothed]
    mad = median(deviations) or 1.0
    threshold = max(8.0, centre + 1.5 * mad)
    candidates: list[int] = []
    for index, value in enumerate(smoothed):
        position = (index + 1) / (len(smoothed) + 1)
        if position < 0.06 or position > 0.94 or value < threshold:
            continue
        left = smoothed[index - 1] if index else value
        right = smoothed[index + 1] if index + 1 < len(smoothed) else value
        if value >= left and value >= right:
            candidates.append(index)
    groups: list[list[int]] = []
    for index in candidates:
        if groups and index - groups[-1][-1] <= max(2, len(values) // 100):
            groups[-1].append(index)
        else:
            groups.append([index])
    peaks: list[dict[str, float]] = []
    for group in groups:
        best = max(group, key=lambda item: smoothed[item])
        peaks.append(
            {
                "position": (best + 1) / (len(smoothed) + 1),
                "score": round(smoothed[best], 4),
                "support_start": round((group[0] + 1) / (len(smoothed) + 1), 6),
                "support_end": round((group[-1] + 1) / (len(smoothed) + 1), 6),
            }
        )
    stats = {
        "minimum": round(min(smoothed), 4),
        "maximum": round(max(smoothed), 4),
        "median": round(centre, 4),
        "threshold": round(threshold, 4),
    }
    return peaks, stats


def _structured_boundaries(edge_values: list[float], ink_values: list[float]) -> tuple[list[dict[str, float]], dict[str, Any]]:
    """Find repeated separators without assuming a particular grid dimension."""
    edge = _smooth(edge_values)
    ink = _smooth(ink_values)
    if not edge or not ink:
        return [], {"candidate_count": None, "confidence": 0.0, "evidence": []}
    edge_centre = median(edge)
    edge_mad = max(1.0, median([abs(value - edge_centre) for value in edge]))
    ink_centre = median(ink)
    ink_mad = max(1.0, median([abs(value - ink_centre) for value in ink]))
    best: tuple[float, int, list[dict[str, float]]] | None = None
    for count in range(2, 7):
        boundaries: list[dict[str, float]] = []
        strengths: list[float] = []
        for boundary_index in range(1, count):
            expected = boundary_index / count
            radius = max(3, round(len(edge) * 0.045 / count))
            centre_index = round(expected * (len(edge) + 1)) - 1
            start = max(0, centre_index - radius)
            end = min(len(edge), centre_index + radius + 1)
            edge_index = max(range(start, end), key=lambda index: edge[index])
            ink_index = min(range(start, end), key=lambda index: ink[index])
            edge_strength = max(0.0, (edge[edge_index] - edge_centre) / edge_mad)
            valley_strength = max(0.0, (ink_centre - ink[ink_index]) / ink_mad)
            strength = max(edge_strength, valley_strength)
            strengths.append(strength)
            boundaries.append(
                {
                    "position": round((edge_index + 1) / (len(edge) + 1), 6),
                    "score": round(strength, 4),
                    "support_start": round(start / max(1, len(edge)), 6),
                    "support_end": round(end / max(1, len(edge)), 6),
                }
            )
        confidence = min(strengths) if strengths else 0.0
        score = confidence + 0.1 * sum(strengths) / max(1, len(strengths))
        if best is None or score > best[0]:
            best = (score, count, boundaries)
    if best is None or min(item["score"] for item in best[2]) < 1.25:
        return [], {"candidate_count": None, "confidence": 0.0, "evidence": []}
    return best[2], {"candidate_count": best[1], "confidence": round(min(1.0, min(item["score"] for item in best[2]) / 4), 6), "evidence": best[2]}


def _edge_density(image: Image.Image) -> float:
    vertical = _axis_profile(image, "vertical")
    horizontal = _axis_profile(image, "horizontal")
    values = vertical + horizontal
    return round(sum(value >= 24 for value in values) / max(1, len(values)), 6)


def _border_signal(image: Image.Image) -> float:
    width, height = image.size
    border_width = max(2, round(min(width, height) * 0.035))
    values = _pixel_values(image)
    border: list[int] = []
    interior: list[int] = []
    for row in range(height):
        for column in range(width):
            target = border if row < border_width or column < border_width or row >= height - border_width or column >= width - border_width else interior
            if column:
                target.append(abs(values[row * width + column] - values[row * width + column - 1]))
            if row:
                target.append(abs(values[row * width + column] - values[(row - 1) * width + column]))
    border_density = sum(value >= 24 for value in border) / max(1, len(border))
    interior_density = sum(value >= 24 for value in interior) / max(1, len(interior))
    return round(min(1.0, max(0.0, (border_density - interior_density) * 5 + 0.5)), 6)


def _grid_regions(vertical: list[dict[str, float]], horizontal: list[dict[str, float]]) -> list[dict[str, Any]]:
    if len(vertical) < 2 or len(horizontal) < 2:
        return []
    x_boundaries = [0.0] + [item["position"] for item in vertical] + [1.0]
    y_boundaries = [0.0] + [item["position"] for item in horizontal] + [1.0]
    regions: list[dict[str, Any]] = []
    for row in range(len(y_boundaries) - 1):
        for column in range(len(x_boundaries) - 1):
            left, right = x_boundaries[column], x_boundaries[column + 1]
            top, bottom = y_boundaries[row], y_boundaries[row + 1]
            if right - left < 0.08 or bottom - top < 0.08:
                continue
            regions.append(
                {
                    "region_id": f"r{row + 1:02d}c{column + 1:02d}",
                    "role": "quiz_card_candidate",
                    "coordinates": {"left": round(left, 6), "top": round(top, 6), "right": round(right, 6), "bottom": round(bottom, 6)},
                    "transform": {"rotation_degrees": 0},
                    "producer": "deterministic_projection_features",
                    "confidence": 0.0,
                }
            )
    return regions


def _longest_edge_run(values: list[int], threshold: int = 18) -> int:
    longest = 0
    current = 0
    for value in values:
        current = current + 1 if value >= threshold else 0
        longest = max(longest, current)
    return longest


def _line_support(image: Image.Image, axis: str, start: float, end: float) -> list[float]:
    edge = image.filter(ImageFilter.FIND_EDGES)
    width, height = edge.size
    pixels = _pixel_values(edge)
    if axis == "vertical":
        return [
            _longest_edge_run([pixels[row * width + column] for row in range(round(start * height), round(end * height))])
            for column in range(width)
        ]
    return [
        _longest_edge_run([pixels[row * width + column] for column in range(round(start * width), round(end * width))])
        for row in range(height)
    ]


def _best_boundary(support: list[float], expected: float, start: float, end: float) -> tuple[float, float]:
    if not support:
        return expected, 0.0
    coordinate_count = len(support)
    window_start = max(0, round((expected - start) / (end - start) * coordinate_count) - max(2, round(coordinate_count * 0.08)))
    window_end = min(coordinate_count, round((expected - start) / (end - start) * coordinate_count) + max(2, round(coordinate_count * 0.08)))
    index = max(range(window_start, max(window_start + 1, window_end)), key=lambda item: support[item])
    position = start + (index + 0.5) / coordinate_count * (end - start)
    return round(position, 6), round(min(1.0, support[index] / 40), 6)


def _composite_regions(image: Image.Image) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Propose mixed aspect-ratio regions from local frame evidence."""
    height_ratio = image.height / max(1, image.width)
    if height_ratio < 1.15:
        return [], None
    horizontal_support = _line_support(image, "horizontal", 0.0, 1.0)
    horizontal_peaks = sorted(
        ((index / max(1, len(horizontal_support)), value) for index, value in enumerate(horizontal_support)),
        key=lambda item: item[1],
        reverse=True,
    )
    row_boundaries: list[float] = []
    for expected in (0.34, 0.72):
        position, confidence = _best_boundary(horizontal_support, expected, 0.12, 0.9)
        if confidence < 0.45:
            return [], None
        row_boundaries.append(position)
    if row_boundaries[1] - row_boundaries[0] < 0.18:
        return [], None
    lower_start, lower_band_confidence = _best_boundary(
        horizontal_support,
        min(0.82, row_boundaries[1] + 0.08),
        row_boundaries[1] + 0.03,
        0.95,
    )
    if lower_band_confidence < 0.55:
        return [], None
    vertical_support = _line_support(image, "vertical", 0.0, row_boundaries[1])
    first_column, first_confidence = _best_boundary(vertical_support, 1 / 3, 0.0, 1.0)
    second_column, second_confidence = _best_boundary(vertical_support, 2 / 3, 0.0, 1.0)
    if first_confidence < 0.45 or second_confidence < 0.45 or second_column - first_column < 0.18:
        return [], None
    lower_support = _line_support(image, "vertical", lower_start, 1.0)
    detected_lower_column, lower_confidence = _best_boundary(lower_support, 0.5, 0.0, 1.0)
    lower_column = 0.5
    if abs(detected_lower_column - lower_column) <= 0.08:
        lower_column = round((detected_lower_column + lower_column) / 2, 6)
    else:
        lower_confidence = 0.45
    x_boundaries = [0.0, first_column, second_column, 1.0]
    y_boundaries = [0.0, row_boundaries[0], row_boundaries[1]]
    regions: list[dict[str, Any]] = []
    for row in range(2):
        for column in range(3):
            regions.append(
                {
                    "region_id": f"vertical_r{row + 1:02d}c{column + 1:02d}",
                    "role": "quiz_card_candidate",
                    "layout_role": "dominant_vertical_grid",
                    "group_id": "dominant_vertical_grid",
                    "orientation": "vertical",
                    "coordinates": {"left": round(x_boundaries[column], 6), "top": round(y_boundaries[row], 6), "right": round(x_boundaries[column + 1], 6), "bottom": round(y_boundaries[row + 1], 6)},
                    "transform": {"rotation_degrees": 0},
                    "producer": "deterministic_composite_frame_features",
                    "confidence": round(min(first_confidence, second_confidence), 6),
                }
            )
    bottom_x = [0.0, lower_column, 1.0]
    for column in range(2):
        regions.append(
            {
                "region_id": f"horizontal_r01c{column + 1:02d}",
                "role": "quiz_card_candidate",
                "layout_role": "secondary_horizontal_pair",
                "group_id": "secondary_horizontal_pair",
                "orientation": "horizontal",
                "coordinates": {"left": round(bottom_x[column], 6), "top": round(lower_start, 6), "right": round(bottom_x[column + 1], 6), "bottom": 1.0},
                    "transform": {"rotation_degrees": 90, "rotation_candidates": [-90, 90]},
                "producer": "deterministic_composite_frame_features",
                "confidence": round(lower_confidence, 6),
            }
        )
    observation = {
        "family": "composite_regions",
        "dominant_layout": {"family": "grid", "orientation": "vertical", "row_count": 2, "column_count": 3, "region_count": 6},
        "secondary_layout": {"family": "pair", "orientation": "horizontal", "row_count": 1, "column_count": 2, "region_count": 2},
        "confidence": round(min(first_confidence, second_confidence, lower_confidence, lower_band_confidence), 6),
        "boundary_evidence": {"horizontal_peaks": horizontal_peaks[:8], "row_boundaries": row_boundaries, "column_boundaries": x_boundaries, "lower_column": lower_column},
        "coordinate_system": "normalized [0, 1] image coordinates",
    }
    return regions, observation


def analyze_source_features(source: dict[str, Any], root: Path) -> dict[str, Any]:
    source_path = root / source["path"]
    image, width, height = _sample_image(source_path)
    vertical_values = _axis_profile(image, "vertical")
    horizontal_values = _axis_profile(image, "horizontal")
    vertical_ink = _ink_profile(image, "vertical")
    horizontal_ink = _ink_profile(image, "horizontal")
    vertical, vertical_stats = _structured_boundaries(vertical_values, vertical_ink)
    horizontal, horizontal_stats = _structured_boundaries(horizontal_values, horizontal_ink)
    if not vertical:
        vertical, vertical_stats = _separator_peaks(vertical_values)
    if not horizontal:
        horizontal, horizontal_stats = _separator_peaks(horizontal_values)
    regions, composite_observation = _composite_regions(image)
    if not regions:
        regions = _grid_regions(vertical, horizontal)
    grid_confidence = 0.0
    if regions:
        grid_confidence = min(1.0, 0.62 + 0.06 * min(6, len(vertical) + len(horizontal) - 4))
        for region in regions:
            region["confidence"] = round(grid_confidence, 6)
    edge_density = _edge_density(image)
    border_signal = _border_signal(image)
    aspect_ratio = round(width / height, 6) if height else None
    outlier_signal = 0.0
    if aspect_ratio is not None:
        outlier_signal = round(max(0.0, min(1.0, abs(aspect_ratio - 0.726) / 0.2)), 6)
    layout_family = "composite_regions" if composite_observation else ("grid" if regions else "unknown")
    if not regions and border_signal >= 0.55:
        layout_family = "single"
    elif not regions:
        layout_family = "irregular_regions"
    feature_summary = {
        "has_grid_evidence": bool(regions),
        "has_single_card_frame_signal": border_signal >= 0.55,
        "possible_non_card_signal": round(max(0.0, min(1.0, 0.55 * outlier_signal + 0.25 * (1 - border_signal) + 0.2 * min(1, edge_density * 4))), 6),
        "multi_region_count": len(regions),
        "irregular_layout_signal": round(1.0 - grid_confidence if not regions else 1.0 - grid_confidence, 6),
    }
    payload: dict[str, Any] = {
        "artifact_type": "source_feature_analysis",
        "implementation_version": IMPLEMENTATION_VERSION,
        "feature_rules_version": FEATURE_RULES_VERSION,
        "created_at_utc": utc_now(),
        "source": {
            "source_id": source["source_id"],
            "member_name": source["member_name"],
            "path": source["path"],
            "sha256": source["file_sha256"],
            "format": source.get("format"),
            "mode": source.get("mode"),
            "width": width,
            "height": height,
        },
        "image_statistics": {
            "sample_width": image.width,
            "sample_height": image.height,
            "aspect_ratio": aspect_ratio,
            "edge_density": edge_density,
            "border_signal": border_signal,
        },
        "projection_evidence": {
            "vertical": {"peaks": vertical, "statistics": vertical_stats, "profile": [round(value, 4) for value in vertical_values], "ink_profile": [round(value, 4) for value in vertical_ink]},
            "horizontal": {"peaks": horizontal, "statistics": horizontal_stats, "profile": [round(value, 4) for value in horizontal_values], "ink_profile": [round(value, 4) for value in horizontal_ink]},
        },
        "layout_observation": {
            "family": layout_family,
            "row_count": composite_observation["dominant_layout"]["row_count"] if composite_observation else (len(horizontal) + 1 if regions else None),
            "column_count": composite_observation["dominant_layout"]["column_count"] if composite_observation else (len(vertical) + 1 if regions else None),
            "row_coefficient": round(1 / composite_observation["dominant_layout"]["row_count"], 6) if composite_observation else (round(1 / (len(horizontal) + 1), 6) if regions else None),
            "column_coefficient": round(1 / composite_observation["dominant_layout"]["column_count"], 6) if composite_observation else (round(1 / (len(vertical) + 1), 6) if regions else None),
            "row_gap_coefficient": round(sum(item["support_end"] - item["support_start"] for item in horizontal) / max(1, len(horizontal)), 6) if regions else None,
            "column_gap_coefficient": round(sum(item["support_end"] - item["support_start"] for item in vertical) / max(1, len(vertical)), 6) if regions else None,
            "confidence": round(grid_confidence if regions else max(0.2, border_signal), 6),
            "coordinate_system": "normalized [0, 1] image coordinates",
            "composite": composite_observation,
        },
        "region_candidates": regions,
        "content_role_signals": {
            "quiz_card_front": round(grid_confidence if regions else border_signal * 0.5, 6),
            "single_card": round(border_signal, 6),
            "game_board": round(feature_summary["possible_non_card_signal"], 6),
            "unknown": round(max(0.0, 1.0 - max(grid_confidence, border_signal)), 6),
        },
        "feature_summary": feature_summary,
    }
    payload["feature_fingerprint"] = object_hash({key: value for key, value in payload.items() if key != "created_at_utc"})
    return payload


def _model_prompt() -> str:
    return (
        "Inspect the labeled source contact sheet and compare each source using the supplied feature summaries. "
        "Return JSON only with sources: [{source_id, candidate_scores, content_roles, visual_state, "
        "layout_family, row_count, column_count, anomalies, reason}]. "
        "Candidate scores must use only card_collection, individual_card, non_card, unknown. "
        "This is evidence, not an authoritative route; do not invent coordinates."
    )


def build_collection_analysis(
    collection: dict[str, Any],
    manifest: dict[str, Any],
    feature_records: list[dict[str, Any]],
    root: Path,
    *,
    model_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feature_by_source = {record["source"]["source_id"]: record for record in feature_records}
    prompt = _model_prompt()
    request = {
        "collection_id": collection["collection_id"],
        "collection_image": {"path": collection["image_path"], "sha256": collection["image_sha256"]},
        "manifest": {"path": collection["manifest_path"], "sha256": collection["manifest_sha256"], "cell_count": manifest["cell_count"]},
        "sources": [
            {
                "source_id": cell["source_id"],
                "member_name": cell["member_name"],
                "cell": {"index": cell["cell_index"], "row": cell["row"], "column": cell["column"]},
                "source_sha256": cell["source_sha256"],
                "feature_fingerprint": feature_by_source.get(cell["source_id"], {}).get("feature_fingerprint"),
                "feature_summary": feature_by_source.get(cell["source_id"], {}).get("feature_summary", {}),
                "layout_observation": feature_by_source.get(cell["source_id"], {}).get("layout_observation", {}),
            }
            for cell in manifest["cells"]
        ],
        "prompt": {"version": MODEL_PROMPT_VERSION, "text": prompt, "sha256": sha256_bytes(prompt.encode("utf-8"))},
        "response_schema": {"sources": "array of source evidence objects; candidate_scores use routing classes only"},
    }
    deterministic_context = {
        "grid_source_count": sum(bool(feature_by_source.get(cell["source_id"], {}).get("region_candidates")) for cell in manifest["cells"]),
        "source_count": len(manifest["cells"]),
    }
    normalized_model = normalize_model_result(model_result, manifest) if model_result is not None else None
    payload = {
        "artifact_type": "collection_visual_evaluation",
        "implementation_version": IMPLEMENTATION_VERSION,
        "created_at_utc": utc_now(),
        "collection": request,
        "deterministic_context": deterministic_context,
        "model": {
            "status": "available" if normalized_model is not None else "not_requested",
            "deployment": os.getenv("KERDEZZ_CLASSIFICATION_DEPLOYMENT") if normalized_model is not None else None,
            "response": normalized_model,
            "response_sha256": object_hash(normalized_model) if normalized_model is not None else None,
        },
    }
    payload["evaluation_fingerprint"] = object_hash({key: value for key, value in payload.items() if key != "created_at_utc"})
    return payload


def normalize_model_result(result: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    allowed_sources = {cell["source_id"] for cell in manifest["cells"]}
    normalized_sources: list[dict[str, Any]] = []
    for item in result.get("sources", []) if isinstance(result, dict) else []:
        if not isinstance(item, dict) or item.get("source_id") not in allowed_sources:
            continue
        scores = {
            name: max(0.0, min(1.0, float(item.get("candidate_scores", {}).get(name, 0.0))))
            for name in ROUTING_CLASSES
        }
        normalized_sources.append(
            {
                "source_id": item["source_id"],
                "candidate_scores": scores,
                "content_roles": [str(role) for role in item.get("content_roles", []) if isinstance(role, str)],
                "visual_state": item.get("visual_state", "unknown") if item.get("visual_state") in {"front", "back", "mixed", "unreadable", "unknown"} else "unknown",
                "layout_family": item.get("layout_family", "unknown"),
                "row_count": item.get("row_count") if isinstance(item.get("row_count"), int) else None,
                "column_count": item.get("column_count") if isinstance(item.get("column_count"), int) else None,
                "anomalies": [str(value) for value in item.get("anomalies", []) if isinstance(value, str)],
                "reason": str(item.get("reason", "")),
            }
        )
    return {"sources": normalized_sources, "producer": "vision_model_evidence", "validated": True}


def _collection_peer_context(source_id: str, feature_records: list[dict[str, Any]]) -> dict[str, Any]:
    peers = [record for record in feature_records if record["source"]["source_id"] != source_id]
    grid_count = sum(bool(record["region_candidates"]) for record in peers)
    return {"peer_count": len(peers), "peer_grid_count": grid_count, "peer_grid_ratio": grid_count / len(peers) if peers else 0.0}


def fuse_classification(
    source: dict[str, Any],
    features: dict[str, Any],
    collection_evaluation: dict[str, Any],
) -> dict[str, Any]:
    source_id = source["source_id"]
    has_grid = bool(features["region_candidates"])
    context = collection_evaluation["deterministic_context"]
    peer_context = _collection_peer_context(source_id, [
        {"source": item["source"], "region_candidates": item.get("region_candidates", [])}
        for item in collection_evaluation.get("feature_records", [])
    ]) if collection_evaluation.get("feature_records") else {"peer_count": 0, "peer_grid_count": 0, "peer_grid_ratio": 0.0}
    if has_grid:
        deterministic_scores = {"card_collection": 0.94, "individual_card": 0.01, "non_card": 0.01, "unknown": 0.04}
    elif context["grid_source_count"] == 0:
        deterministic_scores = {"card_collection": 0.02, "individual_card": 0.78, "non_card": 0.04, "unknown": 0.16}
    elif peer_context["peer_grid_ratio"] >= 0.5 or context["grid_source_count"] >= 1:
        deterministic_scores = {"card_collection": 0.08, "individual_card": 0.08, "non_card": 0.28, "unknown": 0.56}
    else:
        deterministic_scores = {"card_collection": 0.08, "individual_card": 0.2, "non_card": 0.22, "unknown": 0.5}
    model_sources = (collection_evaluation.get("model", {}).get("response") or {}).get("sources", [])
    model_item = next((item for item in model_sources if item["source_id"] == source_id), None)
    final_scores = deterministic_scores.copy()
    if model_item:
        final_scores = {name: round(0.75 * deterministic_scores[name] + 0.25 * model_item["candidate_scores"].get(name, 0.0), 6) for name in ROUTING_CLASSES}
    selected = max(ROUTING_CLASSES, key=lambda name: final_scores[name])
    confidence = final_scores[selected]
    if selected != "unknown" and confidence < 0.6:
        selected = "unknown"
        confidence = final_scores["unknown"]
    roles: list[str]
    if has_grid:
        roles = ["quiz_card_front"]
    elif selected == "individual_card":
        roles = ["quiz_card_front"]
    elif features["feature_summary"]["possible_non_card_signal"] >= 0.45:
        roles = ["game_board"]
    else:
        roles = ["unknown"]
    if model_item:
        roles = list(dict.fromkeys(roles + model_item["content_roles"])) or ["unknown"]
    layout = features["layout_observation"].copy()
    anomalies: list[str] = []
    if features["layout_observation"]["family"] == "composite_regions":
        anomalies.extend(["mixed_orientation", "mixed_aspect_ratio", "non_uniform_spacing"])
    elif not has_grid and context["grid_source_count"]:
        anomalies.extend(["irregular_regions", "mixed_orientation"] if features["feature_summary"]["possible_non_card_signal"] < 0.45 else ["possible_non_card"])
    if model_item:
        anomalies.extend(model_item["anomalies"])
    accepted_regions = features["region_candidates"] if selected == "card_collection" else []
    for region in accepted_regions:
        region["accepted"] = True
    observation = {
        "content_roles": roles,
        "visual_state": model_item["visual_state"] if model_item else "unknown",
        "layout_pattern": layout,
        "region_candidates": features["region_candidates"],
        "anomalies": list(dict.fromkeys(anomalies)),
        "feature_summary": features["feature_summary"],
    }
    return {
        "source_id": source_id,
        "source_sha256": source["file_sha256"],
        "routing_class": selected,
        "candidate_scores": final_scores,
        "confidence": round(confidence, 6),
        "decision_state": "accepted" if selected != "unknown" else "model_uncertain",
        "observation_profile": observation,
        "accepted_region_count": len(accepted_regions),
        "decision_reason": "projection evidence fused with collection context and optional validated model evidence",
        "consistency_checks": {
            "route_has_geometry": selected == "card_collection" and bool(accepted_regions) or selected != "card_collection",
            "unknown_has_no_accepted_regions": selected != "unknown" or not accepted_regions,
            "non_card_has_no_accepted_regions": selected != "non_card" or not accepted_regions,
        },
    }


def run_collection_model(
    request: dict[str, Any],
    image_path: Path,
    *,
    endpoint: str,
    deployment: str,
) -> dict[str, Any]:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    token_provider = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
    client = AzureOpenAI(azure_endpoint=endpoint, azure_ad_token_provider=token_provider, api_version="2025-01-01-preview")
    encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
    completion = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "developer", "content": "Return conservative JSON evidence only. Never invent coordinates."},
            {"role": "user", "content": [{"type": "text", "text": request["prompt"]["text"] + "\nEvidence:\n" + json.dumps(request["sources"], ensure_ascii=False)}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}}]},
        ],
        max_completion_tokens=3000,
        stream=False,
    )
    content = completion.choices[0].message.content or "{}"
    return json.loads(content)


def classify_sources(
    sources: list[dict[str, Any]],
    collections: list[dict[str, Any]],
    root: Path,
    pool_root: Path,
    *,
    enable_llm: bool = False,
    endpoint: str | None = None,
    deployment: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    feature_dir = pool_root / "classification" / "features"
    evaluation_dir = pool_root / "classification" / "collection_evaluations"
    decision_dir = pool_root / "classification" / "decisions"
    available_sources = [source for source in sources if source.get("status") == "available"]
    features: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    errors: list[str] = []
    for source in available_sources:
        try:
            record = analyze_source_features(source, root)
            path = feature_dir / f"{source['source_id']}.json"
            file_hash = atomic_json(path, record)
            record["artifact_ref"] = {"path": relative(path, root), "sha256": file_hash}
            features.append(record)
            artifacts.append(record["artifact_ref"])
        except (OSError, ValueError, KeyError) as error:
            errors.append(f"{source.get('member_name', source.get('source_id'))}: feature analysis failed: {error}")
    feature_by_source = {record["source"]["source_id"]: record for record in features}
    source_by_id = {source["source_id"]: source for source in available_sources}
    evaluations: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for collection in collections:
        manifest_path = root / collection["manifest_path"]
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            collection_features = [feature_by_source[cell["source_id"]] for cell in manifest["cells"] if cell["source_id"] in feature_by_source]
            evaluation = build_collection_analysis(collection, manifest, collection_features, root)
            if enable_llm:
                if not endpoint or not deployment:
                    raise ValueError("LLM enabled but endpoint/deployment is missing")
                model_result = run_collection_model(evaluation["collection"], root / collection["image_path"], endpoint=endpoint, deployment=deployment)
                evaluation = build_collection_analysis(collection, manifest, collection_features, root, model_result=model_result)
            evaluation_path = evaluation_dir / f"{collection['collection_id']}.json"
            evaluation_hash = atomic_json(evaluation_path, evaluation)
            evaluation["artifact_ref"] = {"path": relative(evaluation_path, root), "sha256": evaluation_hash}
            evaluations.append(evaluation)
            artifacts.append(evaluation["artifact_ref"])
            for cell in manifest["cells"]:
                source = source_by_id.get(cell["source_id"])
                feature = feature_by_source.get(cell["source_id"])
                if source is None or feature is None:
                    continue
                fusion_evaluation = dict(evaluation)
                fusion_evaluation["feature_records"] = collection_features
                decision = fuse_classification(source, feature, fusion_evaluation)
                decision.update(
                    {
                        "artifact_type": "classification_decision",
                        "implementation_version": IMPLEMENTATION_VERSION,
                        "classification_rules_version": CLASSIFICATION_RULES_VERSION,
                        "created_at_utc": utc_now(),
                        "classification_layer": "first_level_classification",
                        "collection": {"collection_id": collection["collection_id"], "cell_index": cell["cell_index"], "row": cell["row"], "column": cell["column"]},
                        "collection_evaluation_ref": evaluation["artifact_ref"],
                        "source_feature_ref": feature["artifact_ref"],
                        "replay_key": object_hash({"source": source["file_sha256"], "evaluation": evaluation["evaluation_fingerprint"], "rules": CLASSIFICATION_RULES_VERSION}),
                    }
                )
                decision_path = decision_dir / source["source_id"] / f"{decision['replay_key']}.json"
                decision_hash = atomic_json(decision_path, decision)
                decision["artifact_ref"] = {"path": relative(decision_path, root), "sha256": decision_hash}
                decisions.append(decision)
                artifacts.append(decision["artifact_ref"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            errors.append(f"{collection.get('collection_id')}: collection analysis failed: {error}")
    return features, evaluations, decisions, artifacts, errors