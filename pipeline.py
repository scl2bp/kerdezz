#!/usr/bin/env python3
"""Build an idempotent JSON lineage ledger and text-bearing artifact database."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGES = (
    ("01_zip_archive", "ZIP archive"),
    ("02_source_files", "source files"),
    ("03_collection_images", "collection images"),
    ("04_page_classification_layout", "page classification and basic layout estimation"),
    ("05_layout_fine_tuning", "layout fine tuning"),
    ("06_card_extraction", "card extraction"),
    ("07_orientation_estimation", "orientation estimation"),
    ("08_ocr_extraction", "OCR extraction"),
    ("09_llm_review_refinement", "LLM review and refinement"),
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def try_read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return read_json(path), None
    except (OSError, json.JSONDecodeError) as error:
        return None, str(error)


def rel(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    return read_json(manifest_path)


def stage_payloads(
    pool_id: str,
    root: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    cards_path: Path | None,
    llm_results_path: Path | None,
) -> dict[str, dict[str, Any]]:
    artifacts = manifest.get("artifacts", {})
    source_images = manifest.get("source_images", [])
    archive = manifest.get("archive", {})
    cards = read_json(cards_path) if cards_path and cards_path.is_file() else []
    llm_results, llm_error = (try_read_json(llm_results_path) if llm_results_path else (None, None))
    if not isinstance(llm_results, list):
        llm_results = []

    return {
        "01_zip_archive": {
            "archive": archive,
            "source_manifest": rel(manifest_path, root),
        },
        "02_source_files": {
            "source_directory": manifest.get("source_directory"),
            "source_images": source_images,
        },
        "03_collection_images": {
            "source_contact_sheets": artifacts.get("source_contact_sheets", []),
            "card_contact_sheets": artifacts.get("card_contact_sheets", []),
            "flagged_candidate_contact_sheets": artifacts.get("flagged_candidate_contact_sheets", []),
        },
        "04_page_classification_layout": {
            "classifications": [
                {"source_file": item["source_file"], "layout": item.get("layout"), "width": item["width"], "height": item["height"]}
                for item in source_images
            ],
        },
        "05_layout_fine_tuning": {
            "known_decisions": manifest.get("evaluation", {}).get("known_decisions", []),
            "flagged_candidates": artifacts.get("flagged_candidates", []),
        },
        "06_card_extraction": {
            "status": "available" if cards else "not_run",
            "card_record_count": len(cards),
            "cards_json": rel(cards_path, root) if cards_path and cards_path.is_file() else None,
        },
        "07_orientation_estimation": {
            "status": "targeted_only" if artifacts.get("flagged_candidates") else "not_required_or_pending",
            "candidates": artifacts.get("flagged_candidates", []),
        },
        "08_ocr_extraction": {
            "status": "available" if cards else "not_run",
            "text_record_count": sum(bool(card.get("raw_text") or card.get("clues") or card.get("answer")) for card in cards),
            "cards_json": rel(cards_path, root) if cards_path and cards_path.is_file() else None,
        },
        "09_llm_review_refinement": {
            "status": "available" if llm_results else ("invalid_capture_recoverable" if llm_error else "not_run"),
            "result_count": len(llm_results),
            "results_json": rel(llm_results_path, root) if llm_results_path and llm_results_path.is_file() else None,
            "cache_policy": manifest.get("evaluation", {}).get("llm_review", {}).get("cache_policy"),
            "capture_error": llm_error,
        },
    }


def ensure_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pools (
            pool_id TEXT PRIMARY KEY,
            archive_path TEXT NOT NULL,
            archive_sha256 TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            source_directory TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stages (
            pool_id TEXT NOT NULL REFERENCES pools(pool_id),
            stage_id TEXT NOT NULL,
            stage_order INTEGER NOT NULL,
            stage_name TEXT NOT NULL,
            status TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            output_path TEXT NOT NULL,
            output_sha256 TEXT NOT NULL,
            details_json TEXT NOT NULL,
            PRIMARY KEY (pool_id, stage_id)
        );
        CREATE TABLE IF NOT EXISTS cards (
            pool_id TEXT NOT NULL REFERENCES pools(pool_id),
            card_id TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_row INTEGER,
            source_column INTEGER,
            image_path TEXT NOT NULL,
            category TEXT,
            answer TEXT,
            text TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            orientation TEXT NOT NULL,
            review_status TEXT NOT NULL,
            PRIMARY KEY (pool_id, card_id)
        );
        CREATE TABLE IF NOT EXISTS card_clues (
            pool_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            clue_number INTEGER NOT NULL,
            text TEXT NOT NULL,
            PRIMARY KEY (pool_id, card_id, clue_number),
            FOREIGN KEY (pool_id, card_id) REFERENCES cards(pool_id, card_id)
        );
        CREATE TABLE IF NOT EXISTS llm_reviews (
            pool_id TEXT NOT NULL,
            image_path TEXT NOT NULL,
            image_sha256 TEXT NOT NULL,
            response_json TEXT NOT NULL,
            PRIMARY KEY (pool_id, image_path, image_sha256)
        );
        """
    )
    return connection


def upsert_cards(connection: sqlite3.Connection, pool_id: str, root: Path, cards: list[dict[str, Any]]) -> None:
    for card in cards:
        clues = card.get("clues") or []
        text_parts = [card.get("category", ""), *clues, card.get("answer", "")]
        text = "\n".join(part for part in text_parts if part).strip()
        connection.execute(
            """
            INSERT INTO cards (pool_id, card_id, source_file, source_row, source_column, image_path,
                category, answer, text, raw_text, orientation, review_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pool_id, card_id) DO UPDATE SET
                source_file=excluded.source_file, source_row=excluded.source_row,
                source_column=excluded.source_column, image_path=excluded.image_path,
                category=excluded.category, answer=excluded.answer, text=excluded.text,
                raw_text=excluded.raw_text, orientation=excluded.orientation,
                review_status=excluded.review_status
            """,
            (
                pool_id, card["id"], card["source_file"], card.get("row"), card.get("column"),
                card["image"], card.get("category"), card.get("answer"), text,
                card.get("raw_text", ""), "unknown", "cached_ocr",
            ),
        )
        connection.execute("DELETE FROM card_clues WHERE pool_id = ? AND card_id = ?", (pool_id, card["id"]))
        connection.executemany(
            "INSERT INTO card_clues (pool_id, card_id, clue_number, text) VALUES (?, ?, ?, ?)",
            [(pool_id, card["id"], number, clue) for number, clue in enumerate(clues, 1)],
        )


def build_pool(pool_id: str, manifest_path: Path, root: Path, database_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    cards_path = root / "output" / "cards.json" if pool_id == "original" else None
    llm_results_path = root / "review" / "llm_review_results.json"
    payloads = stage_payloads(pool_id, root, manifest_path, manifest, cards_path, llm_results_path)
    stages_dir = root / "pipeline" / pool_id / "stages"
    stages_dir.mkdir(parents=True, exist_ok=True)
    connection = ensure_database(database_path)
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "INSERT OR REPLACE INTO pools VALUES (?, ?, ?, ?, ?, ?)",
        (pool_id, manifest["archive"]["path"], manifest["archive"]["sha256"], rel(manifest_path, root), manifest["source_directory"], now),
    )
    for order, (stage_id, stage_name) in enumerate(STAGES, 1):
        output_path = stages_dir / f"{stage_id}.json"
        payload = payloads[stage_id]
        fingerprint = json_hash({"pool_id": pool_id, "stage_id": stage_id, "payload": payload})
        old, _ = try_read_json(output_path)
        cached = isinstance(old, dict) and old.get("input_fingerprint") == fingerprint
        stage_document = dict(old) if cached else {
            "schema_version": "1.0",
            "pool_id": pool_id,
            "stage_id": stage_id,
            "stage_name": stage_name,
            "generated_at_utc": now,
            "input_fingerprint": fingerprint,
            "status": "cached" if cached else payload.get("status", "available"),
            "skip_reason": "inputs_and_parameters_unchanged" if cached else None,
            "payload": payload,
        }
        if cached:
            stage_document["status"] = "cached"
            stage_document["skip_reason"] = "inputs_and_parameters_unchanged"
        output_path.write_text(json.dumps(stage_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        connection.execute(
            "INSERT OR REPLACE INTO stages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pool_id, stage_id, order, stage_name, stage_document["status"], fingerprint,
             rel(output_path, root), file_hash(output_path), json.dumps(payload, ensure_ascii=False)),
        )

    cards = read_json(cards_path) if cards_path and cards_path.is_file() else []
    if cards:
        upsert_cards(connection, pool_id, root, cards)
    llm_results, _ = try_read_json(llm_results_path)
    connection.execute("DELETE FROM llm_reviews WHERE pool_id = ?", (pool_id,))
    if isinstance(llm_results, list):
        for result in llm_results:
            image_path = result.get("image", "")
            result_pool = "children" if "/children/" in image_path else "original"
            if result_pool != pool_id:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO llm_reviews VALUES (?, ?, ?, ?)",
                (pool_id, image_path, result.get("image_sha256", ""), json.dumps(result, ensure_ascii=False)),
            )
    connection.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", ("pipeline_hierarchy", json.dumps([name for _, name in STAGES], ensure_ascii=False)))
    connection.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", ("updated_at_utc", now))
    connection.commit()
    connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--database", type=Path, default=Path("output/artifact_database.sqlite3"))
    parser.add_argument("--pool", action="append", choices=("original", "children"), default=["original", "children"])
    args = parser.parse_args()
    root = args.root.resolve()
    for pool_id in dict.fromkeys(args.pool):
        manifest = root / "review" / pool_id / "input_source_metadata.json"
        if not manifest.is_file():
            raise SystemExit(f"missing manifest: {manifest}")
        build_pool(pool_id, manifest, root, (root / args.database).resolve())
    print(root / args.database)


if __name__ == "__main__":
    main()