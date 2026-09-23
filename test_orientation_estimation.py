from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from orientation_estimation import estimate_orientations


def _card(root: Path, rotation: int | None = 0) -> dict[str, object]:
    image_path = root / "card.jpg"
    Image.new("RGB", (48, 80), "white").save(image_path)
    return {
        "card_id": "card-1",
        "transform": {"rotation_degrees": rotation} if rotation is not None else {},
        "image": {"path": "card.jpg", "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest()},
        "image_ref": {"path": "card.jpg"},
        "region": {"proposal_ref": {"path": "proposal.json"}},
    }


def test_vertical_card_is_materialized_as_upright(tmp_path: Path) -> None:
    records, artifacts, errors = estimate_orientations([_card(tmp_path, 0)], tmp_path, tmp_path / "pipeline" / "original")

    assert errors == []
    assert len(records) == 1
    assert records[0]["decision"] == "upright"
    assert records[0]["transform"]["rotation_degrees"] == 0
    assert len(artifacts) == 2
    assert (tmp_path / records[0]["image"]["path"]).is_file()


def test_rotated_proposal_transform_is_preserved_as_evidence(tmp_path: Path) -> None:
    records, _, errors = estimate_orientations([_card(tmp_path, 90)], tmp_path, tmp_path / "pipeline" / "original")

    assert errors == []
    assert records[0]["decision"] == "upright"
    assert records[0]["transform"]["source_extraction_rotation_degrees"] == 90


def test_missing_transform_blocks_orientation_output(tmp_path: Path) -> None:
    records, artifacts, errors = estimate_orientations([_card(tmp_path, None)], tmp_path, tmp_path / "pipeline" / "original")

    assert records == []
    assert artifacts == []
    assert errors == ["card-1: orientation transform is unavailable"]