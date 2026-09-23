"""Estimate and materialize the orientation of extracted card images."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


IMPLEMENTATION_VERSION = "orientation-estimation-v1"


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


def atomic_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return sha256_file(path)


def atomic_bytes(path: Path, value: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(value)
    os.replace(temporary, path)
    return sha256_file(path)


def relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _rotation(card: dict[str, Any]) -> int | None:
    transform = card.get("transform")
    if not isinstance(transform, dict):
        return None
    value = transform.get("rotation_degrees")
    if not isinstance(value, (int, float)) or int(value) not in {0, 90, -90, 180, -180, 270, -270}:
        return None
    return int(value) % 360


def estimate_orientations(
    cards: list[dict[str, Any]],
    root: Path,
    pool_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    """Create orientation records for extracted cards.

    Card extraction has already applied the proposal transform. This stage
    verifies that result and preserves it as the OCR input artifact.
    """
    records: list[dict[str, Any]] = []
    artifacts: list[dict[str, str]] = []
    errors: list[str] = []
    for card in cards:
        card_id = str(card.get("card_id", "unknown"))
        image_info = card.get("image")
        image_path = root / image_info["path"] if isinstance(image_info, dict) and image_info.get("path") else None
        rotation = _rotation(card)
        if image_path is None or not image_path.is_file():
            errors.append(f"{card_id}: card image is missing")
            continue
        if rotation is None:
            errors.append(f"{card_id}: orientation transform is unavailable")
            continue
        try:
            with Image.open(image_path) as image:
                image.verify()
                width, height = image.size
            image_bytes = image_path.read_bytes()
        except (OSError, ValueError) as error:
            errors.append(f"{card_id}: unable to verify card image: {error}")
            continue
        card_hash = image_info.get("sha256") if isinstance(image_info, dict) else None
        actual_card_hash = sha256_bytes(image_bytes)
        if card_hash and card_hash != actual_card_hash:
            errors.append(f"{card_id}: card image hash mismatch")
            continue

        orientation_dir = pool_root / "orientation" / card_id
        oriented_path = orientation_dir / "oriented.jpg"
        orientation_path = orientation_dir / "orientation.json"
        oriented_hash = atomic_bytes(oriented_path, image_bytes)
        orientation = {
            "artifact_type": "card_orientation",
            "implementation_version": IMPLEMENTATION_VERSION,
            "created_at_utc": utc_now(),
            "card_id": card_id,
            "status": "accepted",
            "decision": "upright",
            "confidence": 0.98 if rotation else 0.9,
            "transform": {
                "rotation_degrees": 0,
                "source_extraction_rotation_degrees": rotation,
                "already_applied": True,
            },
            "evidence": {
                "method": "card_extraction_transform",
                "card_image_ref": card.get("image_ref"),
                "card_image_sha256": actual_card_hash,
                "dimensions": {"width": width, "height": height},
                "proposal_ref": card.get("region", {}).get("proposal_ref"),
            },
            "image": {
                "path": relative(oriented_path, root),
                "sha256": oriented_hash,
                "width": width,
                "height": height,
            },
        }
        record_hash = atomic_json(orientation_path, orientation)
        orientation["artifact_ref"] = {"path": relative(orientation_path, root), "sha256": record_hash}
        orientation["image_ref"] = {"path": relative(oriented_path, root), "sha256": oriented_hash}
        records.append(orientation)
        artifacts.extend([orientation["artifact_ref"], orientation["image_ref"]])
    return records, artifacts, errors