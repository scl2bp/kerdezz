from __future__ import annotations

from pathlib import Path

from PIL import Image

from llm_review import build_review_queue, review_records, validate_response


def _ocr(tmp_path: Path) -> dict:
    image_path = tmp_path / "oriented.jpg"
    Image.new("RGB", (40, 60), "white").save(image_path)
    return {
        "card_id": "card-1",
        "status": "model_review_pending",
        "artifact_ref": {"path": "ocr/card-1/ocr.json", "sha256": "ocr-hash"},
        "oriented_image": {"path": "oriented.jpg", "sha256": "image-hash"},
        "raw_text": "FOGALOM\nA torony",
        "lines": [{"text": "A torony", "top": 10, "words": []}],
        "parsed": {"category": "FOGALOM", "clues": ["A torony"], "answer": "TÖRŐ"},
        "confidence": 0.4,
        "warnings": ["low confidence"],
        "parse_status": "partial",
    }


def test_queue_includes_partial_low_confidence_record() -> None:
    record = _ocr(Path("/tmp"))
    queue = build_review_queue([record])
    assert queue[0]["card_id"] == "card-1"
    assert "low_ocr_confidence" in queue[0]["reasons"]


def test_review_accepts_visible_diacritic_correction_without_mutating_ocr(tmp_path: Path) -> None:
    ocr = _ocr(tmp_path)
    card = {"card_id": "card-1", "image_ref": ocr["oriented_image"]}

    def fake_review(image_path: Path, prompt: str, endpoint: str, deployment: str) -> dict:
        return {
            "decision": "corrected",
            "confidence": 0.98,
            "no_invention": True,
            "reason": "The acute accent is visible in the answer glyph.",
            "corrections": [
                {
                    "field": "answer",
                    "old_value": "TÖRŐ",
                    "new_value": "TÖRŐ",
                    "evidence": ["answer word bounding box shows ő"],
                    "confidence": 0.98,
                }
            ],
        }

    reviews, _, errors, summary = review_records(
        [ocr], [card], tmp_path, tmp_path / "pool", enable_model=True, endpoint="endpoint", deployment="deployment", review_fn=fake_review
    )

    assert not errors
    assert summary["verified_count"] == 1
    assert reviews[0]["final_fields"]["answer"] == "TÖRŐ"
    assert ocr["parsed"]["answer"] == "TÖRŐ"


def test_invalid_correction_value_becomes_failed_response() -> None:
    response, errors = validate_response(
        {
            "decision": "corrected",
            "confidence": 0.9,
            "no_invention": True,
            "reason": "correction",
            "corrections": [
                {"field": "answer", "old_value": "X", "new_value": "Y", "evidence": ["box"], "confidence": 0.9}
            ],
        }
    )
    assert errors == []
    assert response is not None
