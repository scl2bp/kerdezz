from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

import ocr_extraction
from ocr_extraction import extract_ocr, parse_card


def _inputs(root: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    image_path = root / "oriented.jpg"
    Image.new("RGB", (100, 200), "white").save(image_path)
    image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    card = {
        "card_id": "card-1",
        "source": {"member_name": "fogalom1.jpg"},
        "image_ref": {"path": "card.jpg", "sha256": "card-hash"},
    }
    orientation = {
        "card_id": "card-1",
        "artifact_ref": {"path": "orientation.json", "sha256": "orientation-hash"},
        "image": {"path": "oriented.jpg", "sha256": image_hash, "width": 100, "height": 200},
    }
    return [card], [orientation]


def test_parse_card_extracts_numbered_clues_and_answer() -> None:
    category, clues, answer, warnings = parse_card(
        "fogalom1.jpg",
        [{"text": "1. Elso clue", "top": 10}, {"text": "2. Masodik clue", "top": 30}],
        "VALASZ",
        100,
    )

    assert category == "FOGALOM"
    assert clues == ["Elso clue", "Masodik clue"]
    assert answer == "VALASZ"
    assert warnings == []


def test_ocr_record_contains_raw_lines_parse_and_lineage(tmp_path: Path, monkeypatch) -> None:
    cards, orientations = _inputs(tmp_path)
    monkeypatch.setattr(
        ocr_extraction,
        "_ocr_image",
        lambda path: (
            "1. Elso clue\n2. Masodik clue\nVALASZ",
            [{"text": "1. Elso clue", "top": 10}, {"text": "2. Masodik clue", "top": 30}],
            "VALASZ",
            100,
            0.91,
            {"engine": "tesseract", "language": "hun"},
        ),
    )

    records, artifacts, errors = extract_ocr(cards, orientations, tmp_path, tmp_path / "pipeline" / "original")

    assert errors == []
    assert len(artifacts) == 1
    assert records[0]["parsed"]["clues"] == ["Elso clue", "Masodik clue"]
    assert records[0]["orientation_ref"]["path"] == "orientation.json"
    assert (tmp_path / records[0]["artifact_ref"]["path"]).is_file()


def test_ocr_without_orientation_is_blocked(tmp_path: Path) -> None:
    records, artifacts, errors = extract_ocr([{"card_id": "card-1"}], [], tmp_path, tmp_path / "pipeline" / "original")

    assert records == []
    assert artifacts == []
    assert errors == ["card-1: orientation record is unavailable"]