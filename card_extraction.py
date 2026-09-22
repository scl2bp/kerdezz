"""Extract standalone card images from accepted layout proposals."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


IMPLEMENTATION_VERSION = "card-extraction-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def atomic_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return sha256_file(path)


def atomic_image(path: Path, image: Image.Image) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    image.save(temporary, format="JPEG", quality=95, optimize=True)
    os.replace(temporary, path)
    return sha256_file(path)


def _pixel_box(proposal: dict[str, Any]) -> tuple[int, int, int, int] | None:
    value = proposal.get("pixel_coordinates")
    if not isinstance(value, dict):
        return None
    names = ("left", "top", "right", "bottom")
    if not all(isinstance(value.get(name), int) for name in names):
        return None
    box = tuple(int(value[name]) for name in names)
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def _rotation(proposal: dict[str, Any]) -> int | None:
    transform = proposal.get("transform")
    if not isinstance(transform, dict):
        return 0
    value = transform.get("rotation_degrees", 0)
    if not isinstance(value, (int, float)) or int(value) not in {0, 90, -90, 180, -180, 270, -270}:
        return None
    return int(value)


def extract_cards(
    sources: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    root: Path,
    pool_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    sources_by_id = {source["source_id"]: source for source in sources}
    cards: list[dict[str, Any]] = []
    artifacts: list[dict[str, str]] = []
    errors: list[str] = []
    for proposal in proposals:
        if proposal.get("status") != "accepted":
            continue
        source_id = proposal.get("source_id")
        source = sources_by_id.get(source_id)
        region_id = str(proposal.get("region_id", "unknown"))
        if source is None:
            errors.append(f"{source_id}:{region_id}: source is unavailable")
            continue
        box = _pixel_box(proposal)
        rotation = _rotation(proposal)
        if box is None:
            errors.append(f"{source_id}:{region_id}: invalid pixel coordinates")
            continue
        if rotation is None:
            errors.append(f"{source_id}:{region_id}: unsupported rotation")
            continue
        source_path = root / source["path"]
        if not source_path.is_file():
            errors.append(f"{source_id}:{region_id}: source image is missing")
            continue
        proposal_key = str(proposal.get("proposal_key") or object_hash({"source": source["file_sha256"], "region": region_id, "box": box}))
        card_id = f"card_{source_id}_{proposal_key[:16]}"
        card_dir = pool_root / "cards" / card_id
        image_path = card_dir / "card.jpg"
        record_path = card_dir / "card.json"
        try:
            with Image.open(source_path) as source_image:
                source_width, source_height = source_image.size
                if box[0] < 0 or box[1] < 0 or box[2] > source_width or box[3] > source_height:
                    raise ValueError("pixel coordinates exceed source bounds")
                card_image = source_image.convert("RGB").crop(box)
                if rotation:
                    card_image = card_image.rotate(-rotation, expand=True)
                image_hash = atomic_image(image_path, card_image)
                card_width, card_height = card_image.size
        except (OSError, ValueError) as error:
            errors.append(f"{source_id}:{region_id}: {error}")
            continue
        card = {
            "artifact_type": "card_image",
            "implementation_version": IMPLEMENTATION_VERSION,
            "created_at_utc": utc_now(),
            "card_id": card_id,
            "status": "accepted",
            "source": {
                "source_id": source_id,
                "member_name": source["member_name"],
                "path": source["path"],
                "sha256": source["file_sha256"],
            },
            "region": {
                "region_id": region_id,
                "proposal_key": proposal_key,
                "proposal_ref": proposal.get("artifact_ref"),
                "coordinates": proposal.get("coordinates"),
                "pixel_coordinates": {name: box[index] for index, name in enumerate(("left", "top", "right", "bottom"))},
            },
            "transform": {"rotation_degrees": rotation},
            "image": {
                "path": relative(image_path, root),
                "sha256": image_hash,
                "width": card_width,
                "height": card_height,
            },
        }
        record_hash = atomic_json(record_path, card)
        card["artifact_ref"] = {"path": relative(record_path, root), "sha256": record_hash}
        card["image_ref"] = {"path": relative(image_path, root), "sha256": image_hash}
        cards.append(card)
        artifacts.extend([card["artifact_ref"], card["image_ref"]])
    return cards, artifacts, errors