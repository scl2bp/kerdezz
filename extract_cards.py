#!/usr/bin/env python3
"""Extract standalone Hungarian cards and OCR their contents from a ZIP archive."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import pytesseract
from PIL import Image, ImageEnhance, ImageOps


GRID_COLUMNS = 3
GRID_ROWS = 3
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
NUMBERED_LINE = re.compile(r"^\s*[^0-9]{0,3}(\d{1,2})[.)]?\s*(.*)$")
SOURCE_CATEGORY_PREFIXES = (
    ("fogalom", "FOGALOM"),
    ("hely", "HELY"),
    ("szem", "SZEMÉLY"),
    ("személy", "SZEMÉLY"),
    ("tábla", "TÁBLA"),
    ("táryg", "TÁRGY"),
    ("valaki valami", "VALAKINEK A VALAMIJE"),
    ("vkivmi", "VALAKINEK A VALAMIJE"),
    ("élőlény", "ÉLŐLÉNY"),
    ("évszám", "ÉVSZÁM"),
)


@dataclass
class Card:
    id: str
    source_file: str
    row: int
    column: int
    image: str
    category: str
    clues: list[str]
    answer: str
    raw_text: str


def natural_key(path: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", path)
    )


def crop_grid(image: Image.Image) -> Iterable[tuple[int, int, Image.Image]]:
    width, height = image.size
    for row in range(GRID_ROWS):
        top = round(height * row / GRID_ROWS)
        bottom = round(height * (row + 1) / GRID_ROWS)
        for column in range(GRID_COLUMNS):
            left = round(width * column / GRID_COLUMNS)
            right = round(width * (column + 1) / GRID_COLUMNS)
            # Keep the card edges while removing the shared sheet borders.
            yield row, column, image.crop((left + 3, top + 3, right - 3, bottom - 3))


def ocr_card(image: Image.Image) -> tuple[str, list[dict[str, int | str]], str, int]:
    enlarged = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
    prepared = ImageEnhance.Contrast(ImageOps.grayscale(enlarged)).enhance(2.0)
    config = "--psm 6"
    raw_text = pytesseract.image_to_string(prepared, lang="hun", config=config)
    data = pytesseract.image_to_data(
        prepared,
        lang="hun",
        config=config,
        output_type=pytesseract.Output.DICT,
    )
    lines: dict[tuple[int, int, int], dict[str, object]] = {}
    for index, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        key = (
            data["block_num"][index],
            data["par_num"][index],
            data["line_num"][index],
        )
        line = lines.setdefault(key, {"words": [], "top": int(data["top"][index])})
        line["words"].append((int(data["left"][index]), text))
        line["top"] = min(int(line["top"]), int(data["top"][index]))
    ordered_lines = [
        {
            "text": " ".join(text for _, text in sorted(line["words"])),
            "top": round(int(line["top"]) / 2),
        }
        for line in lines.values()
    ]
    answer_words: dict[tuple[int, int, int], list[tuple[int, str]]] = {}
    answer_top = image.height
    for index, text in enumerate(data["text"]):
        text = text.strip()
        if not text or int(data["top"][index]) < image.height * 2 * 0.5:
            continue
        if int(data["height"][index]) < 38 or float(data["conf"][index]) < 50:
            continue
        answer_top = min(answer_top, round(int(data["top"][index]) / 2))
        key = (
            data["block_num"][index],
            data["par_num"][index],
            data["line_num"][index],
        )
        answer_words.setdefault(key, []).append((int(data["left"][index]), text))
    answer_lines = [
        " ".join(text for _, text in sorted(words))
        for _, words in sorted(answer_words.items())
    ]
    visual_answer = " ".join(answer_lines).strip()
    return raw_text, ordered_lines, visual_answer, answer_top


def parse_card(
    source_file: str,
    raw_text: str,
    ocr_lines: list[dict[str, int | str]],
    visual_answer: str,
    answer_top: int,
) -> tuple[str, list[str], str]:
    lines = [
        (str(line["text"]).strip(), int(line["top"]))
        for line in ocr_lines
        if str(line["text"]).strip() and int(line["top"]) < answer_top
    ]
    category = next(
        category
        for prefix, category in SOURCE_CATEGORY_PREFIXES
        if source_file.casefold().startswith(prefix)
    )
    clues: list[str] = []
    current: list[str] = []
    started = False
    expected_number = 1
    for line, _ in lines:
        match = NUMBERED_LINE.match(line)
        if match and 1 <= int(match.group(1)) <= 12:
            number = int(match.group(1))
            if number == 1 and not started:
                started = True
            if not started:
                continue
            if current:
                clues.append(join_wrapped_lines(current))
            current = [match.group(2).strip()]
            expected_number = number + 1
        elif started and current:
            current.append(line)
    if current:
        clues.append(join_wrapped_lines(current))

    return category, clues, visual_answer


def join_wrapped_lines(lines: list[str]) -> str:
    text = " ".join(lines).strip()
    return re.sub(r"-\s+", "", text)


def extract(archive_path: Path, output_dir: Path) -> list[Card]:
    images_dir = output_dir / "cards"
    images_dir.mkdir(parents=True, exist_ok=True)
    cards: list[Card] = []

    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(
            (name for name in archive.namelist() if Path(name).suffix.casefold() in IMAGE_SUFFIXES),
            key=natural_key,
        )
        for source_name in names:
            with archive.open(source_name) as source:
                with Image.open(source) as sheet:
                    sheet = sheet.convert("RGB")
                    for row, column, card_image in crop_grid(sheet):
                        card_id = f"{len(cards) + 1:03d}"
                        image_path = images_dir / f"card_{card_id}.jpg"
                        card_image.save(image_path, quality=95, optimize=True)
                        raw_text, ocr_lines, visual_answer, answer_top = ocr_card(card_image)
                        category, clues, answer = parse_card(
                            source_name,
                            raw_text,
                            ocr_lines,
                            visual_answer,
                            answer_top,
                        )
                        cards.append(
                            Card(
                                id=card_id,
                                source_file=source_name,
                                row=row + 1,
                                column=column + 1,
                                image=str(image_path.relative_to(output_dir)),
                                category=category,
                                clues=clues,
                                answer=answer,
                                raw_text=raw_text.strip(),
                            )
                        )
                        print(f"{card_id}: {source_name} [{row + 1},{column + 1}]", flush=True)

    json_path = output_dir / "cards.json"
    json_path.write_text(
        json.dumps([asdict(card) for card in cards], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return cards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="ZIP archive containing 3x3 card sheets")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help="Output directory for cards and cards.json (default: output)",
    )
    args = parser.parse_args()
    cards = extract(args.archive, args.output)
    print(f"Extracted {len(cards)} cards to {args.output}")


if __name__ == "__main__":
    main()