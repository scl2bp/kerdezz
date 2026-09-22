from __future__ import annotations

from pathlib import Path

from PIL import Image

from card_extraction import extract_cards


def _source(root: Path) -> dict[str, object]:
    source_path = root / "source.jpg"
    Image.new("RGB", (100, 80), "white").save(source_path)
    return {
        "source_id": "source-1",
        "member_name": "hely1.jpg",
        "file_sha256": "source-hash",
        "path": "source.jpg",
        "width": 100,
        "height": 80,
    }


def _proposal(status: str = "accepted") -> dict[str, object]:
    return {
        "region_id": "horizontal_r01c01",
        "proposal_key": "proposal-hash",
        "source_id": "source-1",
        "source_sha256": "source-hash",
        "status": status,
        "coordinates": {"left": 0.1, "top": 0.2, "right": 0.9, "bottom": 0.8},
        "pixel_coordinates": {"left": 10, "top": 16, "right": 90, "bottom": 64},
        "transform": {"rotation_degrees": 90},
        "artifact_ref": {"path": "proposal.json", "sha256": "proposal-hash"},
    }


def test_accepted_proposal_creates_hashed_card_with_lineage(tmp_path: Path) -> None:
    source = _source(tmp_path)

    cards, artifacts, errors = extract_cards([source], [_proposal()], tmp_path, tmp_path / "pipeline" / "original")

    assert errors == []
    assert len(cards) == 1
    card = cards[0]
    assert card["source"]["source_id"] == "source-1"
    assert card["region"]["region_id"] == "horizontal_r01c01"
    assert card["transform"] == {"rotation_degrees": 90}
    assert card["image"]["width"] == 48
    assert card["image"]["height"] == 80
    assert len(artifacts) == 2
    assert (tmp_path / card["image"]["path"]).is_file()
    assert (tmp_path / card["artifact_ref"]["path"]).is_file()


def test_rejected_and_missing_source_proposals_do_not_create_cards(tmp_path: Path) -> None:
    source = _source(tmp_path)
    missing_source_proposal = {**_proposal(), "source_id": "missing"}

    cards, artifacts, errors = extract_cards([source], [_proposal("rejected"), missing_source_proposal], tmp_path, tmp_path / "pipeline" / "original")

    assert cards == []
    assert artifacts == []
    assert errors == ["missing:horizontal_r01c01: source is unavailable"]