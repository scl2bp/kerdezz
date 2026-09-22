from __future__ import annotations

import json
import zipfile
from pathlib import Path

from PIL import Image

from contract_validator import validate_master
from master_pipeline import build_master, load_spec


def test_archive_phase_is_safe_and_leaves_downstream_pending(tmp_path: Path) -> None:
    archive = tmp_path / "probe.zip"
    image_path = tmp_path / "card1.jpg"
    Image.new("RGB", (20, 30), "white").save(image_path)
    with zipfile.ZipFile(archive, "w") as output:
        output.write(image_path, "card1.jpg")
        output.writestr("notes.txt", "ignored")
        output.writestr("../escape.jpg", image_path.read_bytes())

    spec = load_spec()
    master, _ = build_master(spec, "original", archive, None, None, "archive", False, tmp_path)

    assert validate_master(master, spec) == []
    assert master["processing"]["stages"][0]["status"] == "available"
    assert master["processing"]["stages"][1]["status"] == "pending"
    assert master["processing"]["stages"][2]["status"] == "pending"
    assert master["processing"]["stages"][0]["quality"]["errors"]


def test_source_phase_materializes_and_records_image_metadata(tmp_path: Path) -> None:
    archive = tmp_path / "probe.zip"
    image_path = tmp_path / "card1.jpg"
    Image.new("RGB", (20, 30), "white").save(image_path)
    with zipfile.ZipFile(archive, "w") as output:
        output.write(image_path, "card1.jpg")

    master, master_path = build_master(load_spec(), "children", archive, None, 1, "sources", False, tmp_path)
    assert master_path.name == "processing_master.json"
    source = master["artifacts"]["sources"][0]
    assert source["status"] == "available"
    assert source["width"] == 20
    assert source["height"] == 30
    assert master["processing"]["stages"][1]["status"] == "available"


def test_collection_phase_creates_contact_sheet_and_cell_manifest(tmp_path: Path) -> None:
    archive = tmp_path / "probe.zip"
    image_paths = []
    for index in range(2):
        image_path = tmp_path / f"card{index + 1}.jpg"
        Image.new("RGB", (20 + index, 30), "white").save(image_path)
        image_paths.append(image_path)
    with zipfile.ZipFile(archive, "w") as output:
        for image_path in image_paths:
            output.write(image_path, image_path.name)

    master, _ = build_master(load_spec(), "original", archive, None, None, "collections", False, tmp_path)
    collection = master["artifacts"]["collections"][0]
    manifest = json.loads((tmp_path / collection["manifest_path"]).read_text(encoding="utf-8"))
    assert master["processing"]["stages"][2]["status"] == "available"
    assert collection["source_count"] == 2
    assert manifest["cell_count"] == 2
    assert len(manifest["cells"]) == 2


def test_collection_phase_paginates_sources_across_contact_sheets(tmp_path: Path) -> None:
    archive = tmp_path / "probe.zip"
    image_path = tmp_path / "card.jpg"
    Image.new("RGB", (20, 30), "white").save(image_path)
    with zipfile.ZipFile(archive, "w") as output:
        for index in range(17):
            output.write(image_path, f"card{index + 1}.jpg")

    master, _ = build_master(load_spec(), "original", archive, None, None, "collections", False, tmp_path)
    collections = master["artifacts"]["collections"]
    assert len(collections) == 2
    assert [item["source_count"] for item in collections] == [16, 1]