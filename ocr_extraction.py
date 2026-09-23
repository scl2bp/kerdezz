"""Extract and parse Hungarian quiz-card text from oriented card images."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageOps


IMPLEMENTATION_VERSION = "ocr-extraction-v1"
OCR_LANGUAGE = "hun"
OCR_CONFIG = "--psm 6"
NUMBERED_LINE = re.compile(r"^\s*[^0-9]{0,3}(\d{1,2})[.)]?\s*(.*)$")
SOURCE_CATEGORY_PREFIXES = (
    ("fogalom", "FOGALOM"),
    ("hely", "HELY"),
    ("szem", "SZEMÉLY"),
    ("személy", "SZEMÉLY"),
    ("tábla", "TÁBLA"),
    ("tárgy", "TÁRGY"),
    ("valaki valami", "VALAKINEK A VALAMIJE"),
    ("vkivmi", "VALAKINEK A VALAMIJE"),
    ("élőlény", "ÉLŐLÉNY"),
    ("évszám", "ÉVSZÁM"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


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


def _category(source_name: str) -> tuple[str, str | None]:
    folded = source_name.casefold()
    for prefix, category in SOURCE_CATEGORY_PREFIXES:
        if folded.startswith(prefix):
            return category, None
    return "UNKNOWN", "source filename has no known category prefix"


def _join_wrapped_lines(lines: list[str]) -> str:
    text = " ".join(lines).strip()
    return re.sub(r"-\s+", "", text)


def parse_card(source_name: str, ocr_lines: list[dict[str, Any]], visual_answer: str, answer_top: int) -> tuple[str, list[str], str, list[str]]:
    warnings: list[str] = []
    category, category_warning = _category(source_name)
    if category_warning:
        warnings.append(category_warning)
    lines = [
        (str(line["text"]).strip(), int(line["top"]))
        for line in ocr_lines
        if str(line.get("text", "")).strip() and int(line.get("top", 0)) < answer_top
    ]
    clues: list[str] = []
    current: list[str] = []
    started = False
    for line, _ in lines:
        match = NUMBERED_LINE.match(line)
        if match and 1 <= int(match.group(1)) <= 12:
            if int(match.group(1)) == 1 and not started:
                started = True
            if not started:
                continue
            if current:
                clues.append(_join_wrapped_lines(current))
            current = [match.group(2).strip()]
        elif started and current:
            current.append(line)
    if current:
        clues.append(_join_wrapped_lines(current))
    if ocr_lines and not clues:
        warnings.append("no numbered clues were parsed")
    if not visual_answer:
        warnings.append("no answer candidate was detected")
    return category, clues, visual_answer, warnings


def _ocr_image(image_path: Path) -> tuple[str, list[dict[str, Any]], str, int, float, dict[str, Any]]:
    import pytesseract

    with Image.open(image_path) as image:
        original_width, original_height = image.size
        enlarged = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
    prepared = ImageEnhance.Contrast(ImageOps.grayscale(enlarged)).enhance(2.0)
    raw_text = pytesseract.image_to_string(prepared, lang=OCR_LANGUAGE, config=OCR_CONFIG)
    data = pytesseract.image_to_data(prepared, lang=OCR_LANGUAGE, config=OCR_CONFIG, output_type=pytesseract.Output.DICT)
    lines: dict[tuple[int, int, int], dict[str, Any]] = {}
    words: list[dict[str, Any]] = []
    confidences: list[float] = []
    for index, value in enumerate(data.get("text", [])):
        text = str(value).strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, TypeError, ValueError):
            confidence = -1.0
        left = int(data["left"][index])
        top = int(data["top"][index])
        height = int(data["height"][index])
        word = {"text": text, "left": round(left / 2), "top": round(top / 2), "width": round(int(data["width"][index]) / 2), "height": round(height / 2), "confidence": round(confidence, 3)}
        words.append(word)
        if confidence >= 0:
            confidences.append(confidence)
        key = (int(data["block_num"][index]), int(data["par_num"][index]), int(data["line_num"][index]))
        line = lines.setdefault(key, {"words": [], "top": round(top / 2)})
        line["words"].append(word)
        line["top"] = min(int(line["top"]), round(top / 2))
    ordered_lines = []
    for line in sorted(lines.values(), key=lambda item: (int(item["top"]), int(item["words"][0]["left"]))):
        ordered_words = sorted(line["words"], key=lambda item: int(item["left"]))
        ordered_lines.append({"text": " ".join(item["text"] for item in ordered_words), "top": int(line["top"]), "words": ordered_words})
    answer_words = [word for word in words if word["top"] >= original_height * 0.5 and word["height"] >= 19 and word["confidence"] >= 50]
    answer_top = min((int(word["top"]) for word in answer_words), default=original_height)
    visual_answer = " ".join(word["text"] for word in sorted(answer_words, key=lambda item: (item["top"], item["left"]))).strip()
    evidence = {"preprocessing": {"scale": 2, "grayscale": True, "contrast": 2.0}, "engine": "tesseract", "engine_version": str(pytesseract.get_tesseract_version()), "language": OCR_LANGUAGE, "config": OCR_CONFIG}
    confidence = round(sum(confidences) / len(confidences) / 100, 6) if confidences else 0.0
    return raw_text.strip(), ordered_lines, visual_answer, answer_top, confidence, evidence


def extract_ocr(
    cards: list[dict[str, Any]],
    orientations: list[dict[str, Any]],
    root: Path,
    pool_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    orientations_by_card = {item.get("card_id"): item for item in orientations}
    records: list[dict[str, Any]] = []
    artifacts: list[dict[str, str]] = []
    errors: list[str] = []
    for card in cards:
        card_id = str(card.get("card_id", "unknown"))
        orientation = orientations_by_card.get(card_id)
        if not orientation:
            errors.append(f"{card_id}: orientation record is unavailable")
            continue
        image_info = orientation.get("image", {})
        image_path = root / image_info["path"] if image_info.get("path") else None
        if image_path is None or not image_path.is_file():
            errors.append(f"{card_id}: oriented image is missing")
            continue
        oriented_hash = sha256_file(image_path)
        if image_info.get("sha256") and image_info["sha256"] != oriented_hash:
            errors.append(f"{card_id}: oriented image hash mismatch")
            continue
        cache_key = object_hash({"oriented_image_sha256": oriented_hash, "language": OCR_LANGUAGE, "config": OCR_CONFIG, "implementation_version": IMPLEMENTATION_VERSION})
        try:
            raw_text, lines, answer, answer_top, confidence, engine = _ocr_image(image_path)
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            errors.append(f"{card_id}: OCR failed: {error}")
            continue
        category, clues, parsed_answer, warnings = parse_card(card.get("source", {}).get("member_name", ""), lines, answer, answer_top)
        if not raw_text:
            warnings.append("OCR returned empty text")
        status = "available" if raw_text and confidence >= 0.35 else "model_review_pending"
        record = {
            "artifact_type": "card_ocr",
            "implementation_version": IMPLEMENTATION_VERSION,
            "created_at_utc": utc_now(),
            "card_id": card_id,
            "status": status,
            "cache_key": cache_key,
            "source": card.get("source"),
            "card_image_ref": card.get("image_ref"),
            "orientation_ref": orientation.get("artifact_ref"),
            "oriented_image": {"path": relative(image_path, root), "sha256": oriented_hash, "width": image_info.get("width"), "height": image_info.get("height")},
            "engine": engine,
            "raw_text": raw_text,
            "lines": lines,
            "parsed": {"category": category, "clues": clues, "answer": parsed_answer},
            "confidence": confidence,
            "warnings": warnings,
            "parse_status": "complete" if clues and parsed_answer else ("partial" if raw_text else "empty"),
        }
        ocr_dir = pool_root / "ocr" / card_id
        record_path = ocr_dir / "ocr.json"
        record_hash = atomic_json(record_path, record)
        record["artifact_ref"] = {"path": relative(record_path, root), "sha256": record_hash}
        records.append(record)
        artifacts.append(record["artifact_ref"])
    return records, artifacts, errors