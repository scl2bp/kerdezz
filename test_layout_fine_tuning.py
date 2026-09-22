from __future__ import annotations

from pathlib import Path

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