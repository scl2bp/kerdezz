from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from layout_fine_tuning import refine_source_layout


def _source() -> dict[str, object]:
    return {
        "source_id": "source-1",
        "member_name": "card.jpg",
        "file_sha256": "source-hash",
        "width": 1000,
        "height": 800,
    }


def _decision(route: str, candidates: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "source_id": "source-1",
        "routing_class": route,
        "artifact_ref": {"path": "decision.json", "sha256": "decision-hash"},
        "observation_profile": {"region_candidates": candidates or []},
    }


def test_individual_card_becomes_one_full_source_proposal() -> None:
    proposals, errors = refine_source_layout(_source(), _decision("individual_card"))

    assert errors == []
    assert len(proposals) == 1
    assert proposals[0]["pixel_coordinates"] == {"left": 0, "top": 0, "right": 1000, "bottom": 800}
    assert proposals[0]["layout_role"] == "individual_card_source"


def test_invalid_and_overlapping_candidates_are_rejected() -> None:
    candidates = [
        {"region_id": "valid", "coordinates": {"left": 0.0, "top": 0.0, "right": 0.5, "bottom": 0.5}, "confidence": 0.8},
        {"region_id": "overlap", "coordinates": {"left": 0.1, "top": 0.1, "right": 0.6, "bottom": 0.6}, "confidence": 0.8},
        {"region_id": "out_of_bounds", "coordinates": {"left": -0.1, "top": 0.0, "right": 0.2, "bottom": 0.2}, "confidence": 0.8},
    ]

    proposals, errors = refine_source_layout(_source(), _decision("card_collection", candidates))

    assert [proposal["region_id"] for proposal in proposals] == ["valid"]
    assert len(errors) == 2


def test_unknown_and_non_card_emit_no_geometry() -> None:
    candidate = {"region_id": "candidate", "coordinates": {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}}

    for route in ("unknown", "non_card"):
        proposals, errors = refine_source_layout(_source(), _decision(route, [candidate]))
        assert proposals == []
        assert errors == []


def test_composite_frame_refinement_tightens_coarse_boundary(tmp_path: Path) -> None:
    source_path = tmp_path / "composite.jpg"
    image = Image.new("RGB", (100, 100), "white")
    ImageDraw.Draw(image).line((10, 49, 90, 49), fill="black", width=2)
    image.save(source_path)
    source = {**_source(), "path": source_path.name, "width": 100, "height": 100}
    candidate = {
        "region_id": "vertical_r01c01",
        "layout_role": "dominant_vertical_grid",
        "coordinates": {"left": 0.1, "top": 0.0, "right": 0.9, "bottom": 0.7},
        "confidence": 0.8,
    }
    decision = {**_decision("card_collection", [candidate]), "observation_profile": {"layout_pattern": {"family": "composite_regions"}, "region_candidates": [candidate]}}

    proposals, errors = refine_source_layout(source, decision, tmp_path)

    assert errors == []
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["original_coordinates"]["bottom"] == 0.7
    assert proposal["refined_coordinates"]["bottom"] == pytest.approx(0.48)
    assert proposal["coordinates"] == proposal["refined_coordinates"]
    assert proposal["pixel_coordinates"]["bottom"] == 48
    assert proposal["refinement_method"] == "local-frame-edge-run-v1"
    assert proposal["boundary_evidence"]["bottom"]["refined"] is True