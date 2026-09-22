#!/usr/bin/env python3
"""Create labeled image collections and a cache-aware source evaluation manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps


THUMBNAIL_SIZE = (260, 220)
LABEL_HEIGHT = 42
CONTACT_COLUMNS = 4
CONTACT_ROWS = 4
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def archive_members(archive_path: Path) -> list[dict[str, object]]:
    members: list[dict[str, object]] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if Path(info.filename).suffix.casefold() not in IMAGE_SUFFIXES:
                continue
            with archive.open(info) as stream:
                image = Image.open(stream)
                members.append(
                    {
                        "source_file": info.filename,
                        "archive_bytes": info.file_size,
                        "width": image.width,
                        "height": image.height,
                        "mode": image.mode,
                    }
                )
    return members


def contact_sheet(
    images: Iterable[tuple[str, Path]],
    output_dir: Path,
    prefix: str,
    columns: int = CONTACT_COLUMNS,
    rows: int = CONTACT_ROWS,
) -> list[str]:
    entries = list(images)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    cell_width = THUMBNAIL_SIZE[0]
    cell_height = THUMBNAIL_SIZE[1] + LABEL_HEIGHT
    per_sheet = columns * rows
    label_font = font(18)
    for sheet_number in range(0, len(entries), per_sheet):
        batch = entries[sheet_number : sheet_number + per_sheet]
        canvas = Image.new(
            "RGB",
            (columns * cell_width, rows * cell_height),
            "white",
        )
        draw = ImageDraw.Draw(canvas)
        for index, (label, path) in enumerate(batch):
            x = (index % columns) * cell_width
            y = (index // columns) * cell_height
            with Image.open(path) as image:
                preview = ImageOps.contain(image.convert("RGB"), THUMBNAIL_SIZE)
            left = x + (cell_width - preview.width) // 2
            top = y + (THUMBNAIL_SIZE[1] - preview.height) // 2
            canvas.paste(preview, (left, top))
            draw.rectangle((x, y + THUMBNAIL_SIZE[1], x + cell_width, y + cell_height), fill="#e9eef5")
            draw.text((x + 8, y + THUMBNAIL_SIZE[1] + 10), label, fill="#101820", font=label_font)
        path = output_dir / f"{prefix}_{sheet_number // per_sheet + 1:03d}.jpg"
        canvas.save(path, quality=93, optimize=True)
        written.append(str(path))
    return written


def original_evaluation() -> dict[str, object]:
    return {
        "policy": "Do not re-evaluate records already marked verified; inspect only flagged records and source-layout exceptions.",
        "status": "targeted_review",
        "known_decisions": [
            {
                "source_file": "hely1.jpg",
                "classification": "mixed_orientation",
                "affected_generated_ids": ["053"],
                "reason": "The bottom row is rotated in the source scan; card_053 needs a rotation-specific correction.",
            },
            {
                "source_file": "tábla1.jpg",
                "classification": "non_card_board",
                "affected_generated_ids": [f"{number:03d}" for number in range(109, 118)],
                "reason": "The source is a game-board image, not a 3x3 card sheet; generated crops are excluded from card data.",
            },
            {
                "source_file": "élőlény1.jpg",
                "classification": "card_sheet_ocr_review",
                "affected_generated_ids": ["202"],
                "reason": "The crop is a card, but the OCR text has visible scan/crop errors; retain the image and raw OCR for targeted review.",
            },
        ],
        "llm_review": {
            "enabled": False,
            "cache_policy": "Use a content-hash keyed response cache; never send unchanged verified images again.",
            "recommended_inputs": ["source_contact_sheets", "card_contact_sheets", "flagged_card_images"],
        },
    }


def source_layout(pool_id: str, source_file: str) -> str:
    if pool_id == "original" and source_file == "tábla1.jpg":
        return "non_card_board"
    if pool_id == "original" and source_file == "hely1.jpg":
        return "mixed_orientation"
    if pool_id == "original":
        return "card_sheet_3x3"
    return "individual_image_review_required"


def targeted_candidates(source_dir: Path, output_dir: Path, existing_json: Path | None) -> list[dict[str, object]]:
    candidates_dir = output_dir / "flagged_candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[dict[str, object]] = []

    hely_path = source_dir / "hely1.jpg"
    if hely_path.is_file():
        with Image.open(hely_path) as image:
            for name, box in (
                ("hely1_bottom_left", (65, 1250, 650, 1755)),
                ("hely1_bottom_right", (690, 1250, 1275, 1755)),
            ):
                crop = image.crop(box)
                for direction, angle in (("cw", -90), ("ccw", 90)):
                    path = candidates_dir / f"{name}_{direction}.jpg"
                    crop.rotate(angle, expand=True).save(path, quality=95, optimize=True)
                    candidates.append(
                        {
                            "case": "card_053_orientation",
                            "candidate": path.name,
                            "source_file": "hely1.jpg",
                            "source_box_xyxy": list(box),
                            "rotation_degrees": angle,
                        }
                    )

    if existing_json and existing_json.is_file():
        current_path = existing_json.parent / "cards" / "card_202.jpg"
        if current_path.is_file():
            with Image.open(source_dir / "élőlény1.jpg") as image:
                recrop_box = (92, 585, 493, 1165)
                path = candidates_dir / "card_202_recrop.jpg"
                image.crop(recrop_box).save(path, quality=95, optimize=True)
                candidates.append(
                    {
                        "case": "card_202_crop_ocr",
                        "candidate": path.name,
                        "source_file": "élőlény1.jpg",
                        "source_box_xyxy": list(recrop_box),
                        "rotation_degrees": 0,
                    }
                )
    return candidates


def build_manifest(
    archive_path: Path,
    source_dir: Path,
    output_dir: Path,
    pool_id: str,
    existing_json: Path | None,
) -> dict[str, object]:
    source_paths = sorted(
        (path for path in source_dir.iterdir() if path.suffix.casefold() in IMAGE_SUFFIXES),
        key=lambda path: tuple(
            int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", path.name)
        ),
    )
    source_items = []
    for path in source_paths:
        with Image.open(path) as image:
            source_items.append(
                {
                    "source_file": path.name,
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "sha256": sha256(path),
                    "layout": source_layout(pool_id, path.name),
                }
            )

    archive_image_members = archive_members(archive_path)
    source_contact = contact_sheet(
        [(path.name, path) for path in source_paths],
        output_dir / "source_contact_sheets",
        "source_collection",
    )
    card_contact: list[str] = []
    flagged_candidates: list[dict[str, object]] = []
    flagged_contact: list[str] = []
    card_count = 0
    if existing_json and existing_json.is_file():
        records = json.loads(existing_json.read_text(encoding="utf-8"))
        card_paths = [
            (f"{record['id']} {Path(record['image']).name}", existing_json.parent / record["image"])
            for record in records
            if (existing_json.parent / record["image"]).is_file()
        ]
        card_count = len(card_paths)
        card_contact = contact_sheet(card_paths, output_dir / "card_contact_sheets", "cards")
        if pool_id == "original":
            flagged_candidates = targeted_candidates(source_dir, output_dir, existing_json)
            candidate_paths = [
                (item["candidate"], output_dir / "flagged_candidates" / item["candidate"])
                for item in flagged_candidates
            ]
            flagged_contact = contact_sheet(
                candidate_paths,
                output_dir / "flagged_candidate_contact_sheets",
                "flagged_candidates",
            )

    return {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pool_id": pool_id,
        "archive": {
            "path": str(archive_path),
            "sha256": sha256(archive_path),
            "image_member_count": len(archive_image_members),
            "members": archive_image_members,
        },
        "source_directory": str(source_dir),
        "source_images": source_items,
        "evaluation": original_evaluation() if pool_id == "original" else {
            "policy": "Review source sheets before card extraction; preserve any future verified records by content hash.",
            "status": "source_inventory_only",
            "llm_review": {
                "enabled": False,
                "cache_policy": "Use a content-hash keyed response cache; do not resend unchanged verified images.",
                "recommended_inputs": ["source_contact_sheets"],
            },
        },
        "artifacts": {
            "source_contact_sheets": source_contact,
            "card_contact_sheets": card_contact,
            "flagged_candidates": flagged_candidates,
            "flagged_candidate_contact_sheets": flagged_contact,
            "existing_card_record_count": card_count,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pool-id", required=True)
    parser.add_argument("--existing-json", type=Path)
    args = parser.parse_args()
    manifest = build_manifest(
        args.archive,
        args.source_dir,
        args.output,
        args.pool_id,
        args.existing_json,
    )
    manifest_path = args.output / "input_source_metadata.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(manifest_path)


if __name__ == "__main__":
    main()