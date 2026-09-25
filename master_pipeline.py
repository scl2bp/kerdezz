#!/usr/bin/env python3
"""Run the archive and source-file phases of the quiz-card pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image

from classification import classify_sources
from card_extraction import extract_cards
from contract_validator import validate_master
from layout_fine_tuning import fine_tune_layouts
from llm_review import review_records
from orientation_estimation import estimate_orientations
from ocr_extraction import extract_ocr


ROOT = Path(__file__).parent
ARCHIVES = {
    "original": ROOT / "KerdezzFelelek_ALAP.zip",
    "children": ROOT / "GyerekKérdezzFelelek.zip",
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
IMPLEMENTATION_VERSION = "archive-source-collection-v1"
NATURAL_PARTS = re.compile(r"(\d+)")
CONTACT_COLUMNS = 4
CONTACT_ROWS = 4
THUMBNAIL_SIZE = (260, 220)
LABEL_HEIGHT = 42


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


def object_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)


def natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in NATURAL_PARTS.split(value)]


def relative(path: Path, root: Path = ROOT) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


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


def create_source_collections(
    sources: list[dict[str, Any]],
    pool_root: Path,
    root: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not sources:
        return [], ["no materialized sources available for collection"]
    collection_dir = pool_root / "collections" / "source"
    collection_dir.mkdir(parents=True, exist_ok=True)
    cell_width = THUMBNAIL_SIZE[0]
    cell_height = THUMBNAIL_SIZE[1] + LABEL_HEIGHT
    from PIL import ImageDraw, ImageFont, ImageOps

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    label_font = ImageFont.truetype(font_path, 18) if Path(font_path).is_file() else ImageFont.load_default()
    collections: list[dict[str, Any]] = []
    errors: list[str] = []
    per_sheet = CONTACT_COLUMNS * CONTACT_ROWS
    for sheet_index, start in enumerate(range(0, len(sources), per_sheet), start=1):
        batch = sources[start : start + per_sheet]
        collection_id = f"source_collection_{sheet_index:03d}"
        collection_path = collection_dir / f"{collection_id}.jpg"
        manifest_path = collection_dir / f"{collection_id}.json"
        canvas = Image.new("RGB", (CONTACT_COLUMNS * cell_width, CONTACT_ROWS * cell_height), "white")
        draw = ImageDraw.Draw(canvas)
        cells: list[dict[str, Any]] = []
        for local_index, source in enumerate(batch):
            if source.get("status") != "available":
                errors.append(f"{source.get('member_name', source.get('source_id'))}: source is not available")
                continue
            source_path = root / source["path"]
            try:
                with Image.open(source_path) as image:
                    preview = ImageOps.contain(image.convert("RGB"), THUMBNAIL_SIZE)
                column = local_index % CONTACT_COLUMNS
                row = local_index // CONTACT_COLUMNS
                x = column * cell_width
                y = row * cell_height
                left = x + (cell_width - preview.width) // 2
                top = y + (THUMBNAIL_SIZE[1] - preview.height) // 2
                canvas.paste(preview, (left, top))
                draw.rectangle((x, y + THUMBNAIL_SIZE[1], x + cell_width, y + cell_height), fill="#e9eef5")
                label = f"{start + local_index + 1:03d} {source['member_name']}"
                draw.text((x + 8, y + THUMBNAIL_SIZE[1] + 10), label[:34], fill="#101820", font=label_font)
                cells.append(
                    {
                        "cell_index": local_index,
                        "source_index": start + local_index,
                        "row": row,
                        "column": column,
                        "label": label,
                        "source_id": source["source_id"],
                        "member_name": source["member_name"],
                        "source_path": source["path"],
                        "source_sha256": source["file_sha256"],
                    }
                )
            except (OSError, ValueError) as error:
                errors.append(f"{source.get('member_name', source.get('source_id'))}: {error}")
        if not cells:
            continue
        canvas.save(collection_path, quality=93, optimize=True)
        manifest = {
            "collection_id": collection_id,
            "collection_type": "source_contact_sheet",
            "image_path": relative(collection_path, root),
            "image_sha256": sha256_file(collection_path),
            "columns": CONTACT_COLUMNS,
            "rows": CONTACT_ROWS,
            "cell_count": len(cells),
            "cells": cells,
        }
        atomic_json(manifest_path, manifest)
        collections.append(
            {
                "collection_id": manifest["collection_id"],
                "image_path": manifest["image_path"],
                "image_sha256": manifest["image_sha256"],
                "manifest_path": relative(manifest_path, root),
                "manifest_sha256": sha256_file(manifest_path),
                "source_count": len(cells),
            }
        )
    return collections, errors


def safe_member_name(name: str) -> tuple[str | None, str | None]:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not normalized or normalized.endswith("/"):
        return None, "unsafe archive member path"
    return "/".join(path.parts), None


def make_stage(stage: dict[str, Any], status: str, *, reason: str, run_id: str, started: str | None = None) -> dict[str, Any]:
    return {
        "stage_id": stage["id"],
        "stage_name": stage["name"],
        "implementation_version": IMPLEMENTATION_VERSION,
        "status": status,
        "cache": {"key": "", "parameters": {}, "reused": False, "source_stage_run": None},
        "input": {"artifact_refs": [], "required_information": stage["input"], "fingerprint": ""},
        "evaluation": {"method": "", "rules": [], "decisions": []},
        "outputs": {"artifact_refs": [], "records": [], "fingerprint": ""},
        "quality": {"confidence": None, "review_required": False, "errors": [], "warnings": []},
        "handoff": {"accepted_refs": [], "pending_refs": [], "rejected_refs": [], "reason": reason},
        "started_at_utc": started,
        "completed_at_utc": utc_now() if started else None,
        "run_id": run_id,
    }


def inventory_archive(archive_path: Path, pool_id: str, root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    archive_hash = sha256_file(archive_path)
    members: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_names: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            normalized, safety_error = safe_member_name(info.filename)
            if safety_error:
                errors.append(f"{info.filename}: {safety_error}")
                continue
            assert normalized is not None
            if normalized in seen_names:
                errors.append(f"{normalized}: duplicate member identity")
                continue
            seen_names.add(normalized)
            suffix = Path(normalized).suffix.casefold()
            descriptor: dict[str, Any] = {
                "member_name": normalized,
                "member_id": sha256_bytes(f"{pool_id}\0{normalized}".encode("utf-8"))[:16],
                "archive_bytes": info.file_size,
                "eligible": suffix in IMAGE_SUFFIXES,
                "suffix": suffix,
                "member_sha256": None,
                "status": "eligible" if suffix in IMAGE_SUFFIXES else "rejected",
            }
            if suffix in IMAGE_SUFFIXES:
                try:
                    content = archive.read(info)
                    descriptor["member_sha256"] = sha256_bytes(content)
                except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                    descriptor["status"] = "rejected"
                    descriptor["error"] = str(error)
                    errors.append(f"{normalized}: unable to read member: {error}")
            members.append(descriptor)
    members.sort(key=lambda item: natural_key(item["member_name"]))
    archive_descriptor = {
        "pool_id": pool_id,
        "archive_path": relative(archive_path, root),
        "archive_sha256": archive_hash,
        "member_count": len(members),
        "eligible_member_count": sum(item["eligible"] and item["status"] == "eligible" for item in members),
        "error_count": len(errors),
    }
    return archive_descriptor, members, errors


def select_members(members: list[dict[str, Any]], source: str | None, limit: int | None) -> list[dict[str, Any]]:
    selected = [item for item in members if item["eligible"] and item["status"] == "eligible"]
    if source:
        selected = [item for item in selected if item["member_name"] == source or Path(item["member_name"]).name == source]
    if limit is not None:
        selected = selected[:limit]
    return selected


def materialize_sources(
    archive_path: Path,
    pool_root: Path,
    selected: list[dict[str, Any]],
    root: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        infos = {safe_member_name(info.filename)[0]: info for info in archive.infolist()}
        for member in selected:
            member_name = member["member_name"]
            source_id = f"{member['member_id']}_{member['member_sha256'][:12]}"
            suffix = member["suffix"] or ".bin"
            output_path = pool_root / "sources" / source_id / f"original{suffix}"
            descriptor: dict[str, Any] = {
                "source_id": source_id,
                "member_name": member_name,
                "member_id": member["member_id"],
                "member_sha256": member["member_sha256"],
                "path": relative(output_path, root),
                "file_sha256": None,
                "archive_bytes": member["archive_bytes"],
                "status": "pending",
            }
            try:
                info = infos.get(member_name)
                if info is None:
                    raise ValueError("member disappeared between inventory and materialization")
                content = archive.read(info)
                if sha256_bytes(content) != member["member_sha256"]:
                    raise ValueError("member hash changed between inventory and materialization")
                file_hash = atomic_bytes(output_path, content)
                with Image.open(output_path) as image:
                    image.verify()
                with Image.open(output_path) as image:
                    descriptor.update(
                        {
                            "file_sha256": file_hash,
                            "format": image.format,
                            "width": image.width,
                            "height": image.height,
                            "mode": image.mode,
                            "status": "available",
                        }
                    )
            except (OSError, ValueError, RuntimeError) as error:
                descriptor.update({"status": "rejected", "error": str(error)})
                errors.append(f"{member_name}: {error}")
            sources.append(descriptor)
    return sources, errors


def build_master(
    spec: dict[str, Any],
    pool_id: str,
    archive_path: Path,
    source: str | None,
    limit: int | None,
    until: str,
    resume: bool,
    root: Path = ROOT,
    enable_llm: bool = False,
    llm_endpoint: str | None = None,
    llm_deployment: str | None = None,
    enable_llm_review: bool = False,
) -> tuple[dict[str, Any], Path]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_root = root / "pipeline" / pool_id / "runs" / run_id
    pool_root = root / "pipeline" / pool_id
    archive_descriptor, members, archive_errors = inventory_archive(archive_path, pool_id, root)
    selected = select_members(members, source, limit)
    selection = {"source": source, "limit": limit, "selected_member_names": [item["member_name"] for item in selected]}
    archive_fingerprint = object_hash({"archive": archive_descriptor, "members": members, "selection": selection})
    archive_stage = make_stage(spec["stages"][0], "available", reason="eligible archive members are addressable", run_id=run_id, started=utc_now())
    archive_stage["input"] = {"artifact_refs": [{"path": relative(archive_path, root), "sha256": archive_descriptor["archive_sha256"]}], "required_information": spec["stages"][0]["input"], "fingerprint": archive_fingerprint}
    archive_stage["evaluation"] = {"method": "deterministic ZIP inventory", "rules": ["safe member path", "image suffix eligibility", "unique member identity", "member content hash"], "decisions": ["eligible image members", "unsupported or corrupt members"]}
    archive_stage["outputs"] = {"artifact_refs": [], "records": members, "fingerprint": object_hash(members)}
    archive_stage["quality"] = {"confidence": 1.0 if not archive_errors else 0.0, "review_required": False, "errors": archive_errors, "warnings": [], "member_count": len(members), "selected_member_count": len(selected)}
    archive_stage["handoff"] = {"accepted_refs": [item["member_id"] for item in selected], "pending_refs": [], "rejected_refs": [item["member_id"] for item in members if item["status"] != "eligible"], "reason": "selected eligible members are ready for source materialization"}
    source_stage = make_stage(spec["stages"][1], "pending", reason="source phase not selected", run_id=run_id)
    sources: list[dict[str, Any]] = []
    source_errors: list[str] = []
    if until in ("sources", "collections", "classification", "fine_tuning", "card_extraction", "orientation", "ocr", "llm_review"):
        source_started = utc_now()
        sources, source_errors = materialize_sources(archive_path, pool_root, selected, root)
        source_status = "available" if not source_errors else "failed"
        source_stage = make_stage(spec["stages"][1], source_status, reason="all selected sources materialized and decoded" if not source_errors else "one or more selected sources failed", run_id=run_id, started=source_started)
        source_stage["input"] = {"artifact_refs": [{"stage_id": "zip_archive", "fingerprint": archive_fingerprint}], "required_information": spec["stages"][1]["input"], "fingerprint": archive_fingerprint}
        source_stage["evaluation"] = {"method": "deterministic materialization and Pillow decode", "rules": ["safe output path", "member hash equality", "image verification", "dimensions and mode recorded"], "decisions": ["file readable", "file identity stable"]}
        source_stage["outputs"] = {"artifact_refs": [{"path": item["path"], "sha256": item["file_sha256"]} for item in sources if item["file_sha256"]], "records": sources, "fingerprint": object_hash(sources)}
        source_stage["quality"] = {"confidence": 1.0 if not source_errors else 0.0, "review_required": False, "errors": source_errors, "warnings": [], "source_count": len(sources)}
        source_stage["handoff"] = {"accepted_refs": [item["source_id"] for item in sources if item["status"] == "available"], "pending_refs": [], "rejected_refs": [item["source_id"] for item in sources if item["status"] != "available"], "reason": "decodable source descriptors are ready for collections"}
    collection_stage = make_stage(spec["stages"][2], "pending", reason="collection phase not selected", run_id=run_id)
    collections: list[dict[str, Any]] = []
    collection_errors: list[str] = []
    if until in ("collections", "classification", "fine_tuning", "card_extraction", "orientation", "ocr", "llm_review"):
        collection_started = utc_now()
        collections, collection_errors = create_source_collections(sources, pool_root, root)
        collection_status = "available" if collections and not collection_errors else "failed"
        collection_stage = make_stage(spec["stages"][2], collection_status, reason="source contact sheet and cell manifest are available" if collection_status == "available" else "collection creation failed", run_id=run_id, started=collection_started)
        collection_stage["input"] = {"artifact_refs": [{"stage_id": "source_files", "fingerprint": source_stage["outputs"]["fingerprint"]}], "required_information": spec["stages"][2]["input"], "fingerprint": source_stage["outputs"]["fingerprint"]}
        collection_stage["evaluation"] = {"method": "deterministic labeled contact-sheet generation", "rules": ["every available source appears once", "source hash copied into cell manifest", "contact-sheet and manifest hashes recorded"], "decisions": ["collection grouping"]}
        collection_stage["outputs"] = {"artifact_refs": [{"path": item["image_path"], "sha256": item["image_sha256"]} for item in collections] + [{"path": item["manifest_path"], "sha256": item["manifest_sha256"]} for item in collections], "records": collections, "fingerprint": object_hash(collections)}
        collection_stage["quality"] = {"confidence": 1.0 if not collection_errors else 0.0, "review_required": False, "errors": collection_errors, "warnings": [], "source_count": len(sources), "cell_count": sum(item["source_count"] for item in collections)}
        collection_stage["handoff"] = {"accepted_refs": [item["collection_id"] for item in collections], "pending_refs": [], "rejected_refs": [], "reason": "collection evidence is ready for page classification"}
    classification_stage = make_stage(spec["stages"][3], "pending", reason="classification phase not selected", run_id=run_id)
    classification_features: list[dict[str, Any]] = []
    classification_evaluations: list[dict[str, Any]] = []
    classification_decisions: list[dict[str, Any]] = []
    classification_artifacts: list[dict[str, Any]] = []
    classification_regions: list[dict[str, Any]] = []
    classification_errors: list[str] = []
    if until in ("classification", "fine_tuning", "card_extraction", "orientation", "ocr", "llm_review"):
        classification_started = utc_now()
        if collection_stage["status"] != "available":
            classification_stage = make_stage(spec["stages"][3], "pending", reason="classification blocked by collection phase", run_id=run_id, started=classification_started)
        else:
            classification_features, classification_evaluations, classification_decisions, classification_artifacts, classification_errors = classify_sources(
                sources,
                collections,
                root,
                pool_root,
                enable_llm=enable_llm,
                endpoint=llm_endpoint,
                deployment=llm_deployment,
            )
            decision_by_source = {decision["source_id"]: decision for decision in classification_decisions}
            for source_descriptor in sources:
                decision = decision_by_source.get(source_descriptor["source_id"])
                if decision:
                    source_descriptor["classification"] = decision
                    for candidate in decision["observation_profile"]["region_candidates"]:
                        if candidate.get("accepted"):
                            region = dict(candidate)
                            region["region_id"] = f"{source_descriptor['source_id']}:{candidate['region_id']}"
                            region["source_id"] = source_descriptor["source_id"]
                            region["source_sha256"] = source_descriptor["file_sha256"]
                            region["classification_decision_ref"] = decision["artifact_ref"]
                            classification_regions.append(region)
            classification_status = "available" if classification_decisions and not classification_errors else "failed"
            classification_stage = make_stage(
                spec["stages"][3],
                classification_status,
                reason="source features and classification decisions are available" if classification_status == "available" else "one or more classifications failed",
                run_id=run_id,
                started=classification_started,
            )
            classification_input_fingerprint = object_hash(
                {
                    "sources": source_stage["outputs"]["fingerprint"],
                    "collections": collection_stage["outputs"]["fingerprint"],
                    "rules": "observation-fusion-v1",
                    "llm": {"enabled": enable_llm, "endpoint": llm_endpoint, "deployment": llm_deployment},
                }
            )
            classification_stage["cache"] = {"key": classification_input_fingerprint, "parameters": {"llm_enabled": enable_llm}, "reused": False, "source_stage_run": None}
            classification_stage["input"] = {
                "artifact_refs": [{"stage_id": "source_files", "fingerprint": source_stage["outputs"]["fingerprint"]}, {"stage_id": "collection_images", "fingerprint": collection_stage["outputs"]["fingerprint"]}],
                "required_information": spec["stages"][3]["input"],
                "fingerprint": classification_input_fingerprint,
            }
            classification_stage["evaluation"] = {
                "method": "deterministic source projections and collection-context fusion with optional vision evidence",
                "rules": ["source feature artifact is content-hash keyed", "model output is evidence only", "unknown and non-card have zero accepted regions", "geometry is emitted only for card_collection"],
                "decisions": ["routing class", "observation profile", "candidate scores", "region acceptance", "consistency checks"],
            }
            classification_stage["outputs"] = {
                "artifact_refs": classification_artifacts,
                "records": classification_decisions,
                "fingerprint": object_hash(classification_decisions),
            }
            classification_stage["quality"] = {
                "confidence": round(sum(item["confidence"] for item in classification_decisions) / len(classification_decisions), 6) if classification_decisions else 0.0,
                "review_required": any(item["routing_class"] == "unknown" for item in classification_decisions),
                "errors": classification_errors,
                "warnings": [f"{sum(item['routing_class'] == 'unknown' for item in classification_decisions)} source(s) remain unknown"],
                "source_count": len(classification_decisions),
                "feature_artifact_count": len(classification_features),
                "collection_evaluation_count": len(classification_evaluations),
            }
            classification_stage["handoff"] = {
                "accepted_refs": [item["source_id"] for item in classification_decisions if item["routing_class"] in {"card_collection", "individual_card"}],
                "pending_refs": [item["source_id"] for item in classification_decisions if item["routing_class"] == "unknown"],
                "rejected_refs": [item["source_id"] for item in classification_decisions if item["routing_class"] == "non_card"],
                "reason": "only card_collection region candidates proceed to geometry; individual_card proceeds without a sheet grid",
            }
    layout_stage = make_stage(spec["stages"][4], "pending", reason="layout fine-tuning phase not selected", run_id=run_id)
    layout_proposals: list[dict[str, Any]] = []
    layout_artifacts: list[dict[str, Any]] = []
    layout_errors: list[str] = []
    if until in ("fine_tuning", "card_extraction", "orientation", "ocr", "llm_review"):
        layout_started = utc_now()
        if classification_stage["status"] != "available":
            layout_stage = make_stage(spec["stages"][4], "pending", reason="layout fine-tuning blocked by classification phase", run_id=run_id, started=layout_started)
        else:
            layout_proposals, layout_artifacts, layout_errors = fine_tune_layouts(sources, classification_decisions, root, pool_root)
            layout_status = "available" if layout_proposals and not layout_errors else ("available" if not layout_errors else "failed")
            layout_stage = make_stage(
                spec["stages"][4],
                layout_status,
                reason="validated region proposals are available" if layout_status == "available" else "one or more region proposals failed validation",
                run_id=run_id,
                started=layout_started,
            )
            layout_input_fingerprint = object_hash({"classification": classification_stage["outputs"]["fingerprint"], "rules": "normalized-bounds-overlap-v1"})
            layout_stage["cache"] = {"key": layout_input_fingerprint, "parameters": {}, "reused": False, "source_stage_run": None}
            layout_stage["input"] = {"artifact_refs": [{"stage_id": "page_classification", "fingerprint": classification_stage["outputs"]["fingerprint"]}], "required_information": spec["stages"][4]["input"], "fingerprint": layout_input_fingerprint}
            layout_stage["evaluation"] = {"method": "deterministic normalized geometry validation", "rules": ["coordinates are in [0, 1]", "pixel coordinates are in source bounds", "unexpected overlap is rejected", "unknown and non_card emit no proposals"], "decisions": ["accepted region", "rejected region", "rotation candidates", "pixel crop bounds"]}
            layout_stage["outputs"] = {"artifact_refs": layout_artifacts, "records": layout_proposals, "fingerprint": object_hash(layout_proposals)}
            layout_stage["quality"] = {"confidence": round(sum(item.get("confidence", 0.0) for item in layout_proposals) / len(layout_proposals), 6) if layout_proposals else 0.0, "review_required": False, "errors": layout_errors, "warnings": [], "proposal_count": len(layout_proposals)}
            layout_stage["handoff"] = {"accepted_refs": [item["region_id"] for item in layout_proposals], "pending_refs": [], "rejected_refs": [], "reason": "accepted region proposals are ready for card extraction"}
    card_stage = make_stage(spec["stages"][5], "pending", reason="card extraction phase not selected", run_id=run_id)
    cards: list[dict[str, Any]] = []
    card_artifacts: list[dict[str, str]] = []
    card_errors: list[str] = []
    if until in ("card_extraction", "orientation", "ocr", "llm_review"):
        extraction_started = utc_now()
        if layout_stage["status"] != "available":
            card_stage = make_stage(spec["stages"][5], "pending", reason="card extraction blocked by layout fine tuning", run_id=run_id, started=extraction_started)
        else:
            cards, card_artifacts, card_errors = extract_cards(sources, layout_proposals, root, pool_root)
            card_status = "available" if not card_errors else "failed"
            card_stage = make_stage(
                spec["stages"][5],
                card_status,
                reason="standalone card images are available" if card_status == "available" else "one or more card extractions failed",
                run_id=run_id,
                started=extraction_started,
            )
            card_input_fingerprint = object_hash({"layout": layout_stage["outputs"]["fingerprint"], "rules": "accepted-region-crop-v1"})
            card_stage["cache"] = {"key": card_input_fingerprint, "parameters": {}, "reused": False, "source_stage_run": None}
            card_stage["input"] = {"artifact_refs": [{"stage_id": "layout_fine_tuning", "fingerprint": layout_stage["outputs"]["fingerprint"]}], "required_information": spec["stages"][5]["input"], "fingerprint": card_input_fingerprint}
            card_stage["evaluation"] = {"method": "deterministic accepted-region image extraction", "rules": ["only accepted proposals are extracted", "source and proposal lineage is retained", "pixel coordinates stay within source bounds", "image hashes are recorded"], "decisions": ["accepted card", "rejected crop"]}
            card_stage["outputs"] = {"artifact_refs": card_artifacts, "records": cards, "fingerprint": object_hash(cards)}
            card_stage["quality"] = {"confidence": 1.0 if cards and not card_errors else 0.0, "review_required": False, "errors": card_errors, "warnings": [], "card_count": len(cards)}
            card_stage["handoff"] = {"accepted_refs": [card["card_id"] for card in cards], "pending_refs": [], "rejected_refs": [], "reason": "standalone card images are ready for orientation estimation"}
    orientation_stage = make_stage(spec["stages"][6], "pending", reason="orientation estimation phase not selected", run_id=run_id)
    orientations: list[dict[str, Any]] = []
    orientation_artifacts: list[dict[str, str]] = []
    orientation_errors: list[str] = []
    if until in ("orientation", "ocr", "llm_review"):
        orientation_started = utc_now()
        if card_stage["status"] != "available":
            orientation_stage = make_stage(spec["stages"][6], "pending", reason="orientation estimation blocked by card extraction", run_id=run_id, started=orientation_started)
        else:
            orientations, orientation_artifacts, orientation_errors = estimate_orientations(cards, root, pool_root)
            orientation_status = "available" if not orientation_errors else "failed"
            orientation_stage = make_stage(
                spec["stages"][6],
                orientation_status,
                reason="oriented card image references are available" if orientation_status == "available" else "one or more card orientations failed",
                run_id=run_id,
                started=orientation_started,
            )
            orientation_input_fingerprint = object_hash({"cards": card_stage["outputs"]["fingerprint"], "rules": "card-extraction-transform-v1"})
            orientation_stage["cache"] = {"key": orientation_input_fingerprint, "parameters": {}, "reused": False, "source_stage_run": None}
            orientation_stage["input"] = {"artifact_refs": [{"stage_id": "card_extraction", "fingerprint": card_stage["outputs"]["fingerprint"]}], "required_information": spec["stages"][6]["input"], "fingerprint": orientation_input_fingerprint}
            orientation_stage["evaluation"] = {"method": "deterministic card-image verification and extraction-transform lineage", "rules": ["card image exists and verifies", "card image hash matches extraction record", "proposal transform is supported", "extraction rotation is not applied twice"], "decisions": ["upright card", "orientation evidence", "blocked card"]}
            orientation_stage["outputs"] = {"artifact_refs": orientation_artifacts, "records": orientations, "fingerprint": object_hash(orientations)}
            orientation_stage["quality"] = {"confidence": round(sum(item["confidence"] for item in orientations) / len(orientations), 6) if orientations else 0.0, "review_required": bool(orientation_errors), "errors": orientation_errors, "warnings": [], "card_count": len(orientations)}
            orientation_stage["handoff"] = {"accepted_refs": [item["card_id"] for item in orientations], "pending_refs": [], "rejected_refs": [], "reason": "verified oriented card images are ready for OCR"}
            orientation_by_card = {item["card_id"]: item for item in orientations}
            for card in cards:
                orientation = orientation_by_card.get(card["card_id"])
                if orientation:
                    card["orientation_ref"] = orientation["artifact_ref"]
    ocr_stage = make_stage(spec["stages"][7], "pending", reason="OCR extraction phase not selected", run_id=run_id)
    ocr_records: list[dict[str, Any]] = []
    ocr_artifacts: list[dict[str, str]] = []
    ocr_errors: list[str] = []
    if until in ("ocr", "llm_review"):
        ocr_started = utc_now()
        if orientation_stage["status"] != "available":
            ocr_stage = make_stage(spec["stages"][7], "pending", reason="OCR extraction blocked by orientation estimation", run_id=run_id, started=ocr_started)
        else:
            ocr_records, ocr_artifacts, ocr_errors = extract_ocr(cards, orientations, root, pool_root)
            ocr_status = "available" if not ocr_errors else "failed"
            ocr_stage = make_stage(
                spec["stages"][7],
                ocr_status,
                reason="structured OCR artifacts are available" if ocr_status == "available" else "one or more OCR extractions failed",
                run_id=run_id,
                started=ocr_started,
            )
            ocr_input_fingerprint = object_hash({"orientation": orientation_stage["outputs"]["fingerprint"], "rules": "hungarian-tesseract-pipeline-v1"})
            ocr_stage["cache"] = {"key": ocr_input_fingerprint, "parameters": {"language": "hun", "config": "--psm 6"}, "reused": False, "source_stage_run": None}
            ocr_stage["input"] = {"artifact_refs": [{"stage_id": "orientation_estimation", "fingerprint": orientation_stage["outputs"]["fingerprint"]}], "required_information": spec["stages"][7]["input"], "fingerprint": ocr_input_fingerprint}
            ocr_stage["evaluation"] = {"method": "Tesseract Hungarian OCR with deterministic line grouping and quiz parser", "rules": ["only verified oriented images are processed", "raw OCR is retained", "line and word evidence is retained", "parser warnings are explicit", "low-confidence text remains review-pending"], "decisions": ["category", "ordered clues", "answer candidate", "parse status", "text quality warnings"]}
            ocr_stage["outputs"] = {"artifact_refs": ocr_artifacts, "records": ocr_records, "fingerprint": object_hash(ocr_records)}
            ocr_stage["quality"] = {"confidence": round(sum(item["confidence"] for item in ocr_records) / len(ocr_records), 6) if ocr_records else 0.0, "review_required": any(item["status"] != "available" for item in ocr_records) or bool(ocr_errors), "errors": ocr_errors, "warnings": [f"{sum(item['status'] != 'available' for item in ocr_records)} OCR record(s) require review"], "card_count": len(ocr_records)}
            ocr_stage["handoff"] = {"accepted_refs": [item["card_id"] for item in ocr_records if item["status"] == "available"], "pending_refs": [item["card_id"] for item in ocr_records if item["status"] != "available"], "rejected_refs": [], "reason": "OCR artifacts are ready for LLM review"}
            ocr_by_card = {item["card_id"]: item for item in ocr_records}
            for card in cards:
                ocr = ocr_by_card.get(card["card_id"])
                if ocr:
                    card["ocr_ref"] = ocr["artifact_ref"]
    review_stage = make_stage(spec["stages"][8], "pending", reason="LLM review phase not selected", run_id=run_id)
    reviews: list[dict[str, Any]] = []
    review_artifacts: list[dict[str, str]] = []
    review_errors: list[str] = []
    review_summary: dict[str, Any] = {"queue_count": 0, "reviewed_count": 0, "pending_count": 0}
    if until == "llm_review":
        review_started = utc_now()
        if ocr_stage["status"] != "available":
            review_stage = make_stage(spec["stages"][8], "pending", reason="LLM review blocked by OCR extraction", run_id=run_id, started=review_started)
        else:
            reviews, review_artifacts, review_errors, review_summary = review_records(
                ocr_records,
                cards,
                root,
                pool_root,
                enable_model=enable_llm_review,
                endpoint=llm_endpoint,
                deployment=llm_deployment,
            )
            review_status = "available" if review_summary["pending_count"] == 0 else "model_review_pending"
            review_stage = make_stage(
                spec["stages"][8],
                review_status,
                reason="all quiz OCR records have terminal review statuses" if review_status == "available" else "quiz OCR records remain queued for model review",
                run_id=run_id,
                started=review_started,
            )
            review_input_fingerprint = object_hash({"ocr": ocr_stage["outputs"]["fingerprint"], "rules": "full-quiz-review-v2", "model": {"enabled": enable_llm_review, "deployment": llm_deployment}})
            review_stage["cache"] = {"key": review_input_fingerprint, "parameters": {"model_enabled": enable_llm_review, "deployment": llm_deployment}, "reused": False, "source_stage_run": None}
            review_stage["input"] = {"artifact_refs": [{"stage_id": "ocr_extraction", "fingerprint": ocr_stage["outputs"]["fingerprint"]}], "required_information": spec["stages"][8]["input"], "fingerprint": review_input_fingerprint}
            review_stage["evaluation"] = {"method": "one schema-validated Hungarian visual review per quiz card", "rules": ["every quiz OCR record enters the queue exactly once", "category classification and domain content are recorded as audit findings", "only corrections supported by visible pixels can change final OCR fields", "Hungarian diacritics are preserved", "raw OCR remains immutable", "malformed responses become failed review events"], "decisions": ["verified", "corrected", "model_uncertain", "rejected", "failed"]}
            review_stage["outputs"] = {"artifact_refs": review_artifacts, "records": reviews, "fingerprint": object_hash(reviews)}
            review_stage["quality"] = {"confidence": round(sum(item["response"].get("confidence", 0.0) for item in reviews) / len(reviews), 6) if reviews else (1.0 if not review_summary["queue_count"] else 0.0), "review_required": review_summary["pending_count"] > 0, "errors": review_errors, "warnings": [f"{review_summary['pending_count']} review record(s) remain queued"], **review_summary}
            review_stage["handoff"] = {"accepted_refs": [item["card_id"] for item in cards if item.get("final_status") in {"verified", "rejected", "failed", "model_uncertain"}], "pending_refs": [item["card_id"] for item in cards if item.get("final_status") == "model_review_pending"], "rejected_refs": [item["card_id"] for item in cards if item.get("final_status") == "rejected"], "reason": "terminal card review statuses are ready for contract validation" if review_status == "available" else "model review is incomplete"}
    stages = [archive_stage, source_stage, collection_stage, classification_stage, layout_stage, card_stage, orientation_stage, ocr_stage, review_stage]
    for stage in spec["stages"][9:]:
        stages.append(make_stage(stage, "pending", reason=f"blocked until {stage['id']} is implemented", run_id=run_id))
    master = {
        "schema_version": spec["schema_version"],
        "artifact_type": spec["artifact_type"],
        "pool_id": pool_id,
        "input": {"archive_path": relative(archive_path, root), "archive_sha256": archive_descriptor["archive_sha256"], "selection": selection},
        "processing": {"stage_order": [stage["id"] for stage in spec["stages"]], "stages": stages},
        "artifacts": {"sources": sources, "collections": collections, "classifications": classification_decisions, "classification_features": classification_features, "classification_evaluations": classification_evaluations, "regions": layout_proposals or classification_regions, "cards": cards, "orientations": orientations, "ocr": ocr_records, "reviews": reviews},
        "quality_summary": {"archive_errors": len(archive_errors), "source_errors": len(source_errors), "collection_errors": len(collection_errors), "classification_errors": len(classification_errors), "layout_errors": len(layout_errors), "card_errors": len(card_errors), "orientation_errors": len(orientation_errors), "ocr_errors": len(ocr_errors), "review_errors": len(review_errors), "selected_member_count": len(selected)},
        "run": {"run_id": run_id, "started_at_utc": run_id.split("-", 1)[0], "until": until, "resume": resume},
    }
    errors = validate_master(master, spec)
    if errors:
        raise ValueError("generated master failed contract validation: " + "; ".join(errors))
    run_manifest = {"run_id": run_id, "pool_id": pool_id, "until": until, "selection": selection, "archive": archive_descriptor, "selected_members": selected, "source_count": len(sources), "source_errors": source_errors}
    atomic_json(run_root / "run_manifest.json", run_manifest)
    atomic_json(pool_root / "processing_master.json", master)
    return master, pool_root / "processing_master.json"


def load_spec() -> dict[str, Any]:
    return json.loads((ROOT / "pipeline_spec.json").read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", choices=sorted(ARCHIVES), required=True)
    parser.add_argument("--source")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--until", choices=("archive", "sources", "collections", "classification", "fine_tuning", "card_extraction", "orientation", "ocr", "llm_review"), default="sources")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--llm", action="store_true", help="Enable the optional Azure vision evaluation during classification.")
    parser.add_argument("--llm-endpoint", default=os.getenv("ENDPOINT_URL", "https://ae-oa-d-we-004.openai.azure.com/"))
    parser.add_argument("--llm-deployment", default=os.getenv("DEPLOYMENT_NAME", "gpt-5.6-luna"))
    parser.add_argument("--llm-review", action="store_true", help="Enable Azure vision review for every extracted quiz card.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    archive_path = ARCHIVES[args.pool]
    if not archive_path.is_file():
        parser.error(f"archive does not exist: {archive_path}")
    master, master_path = build_master(
        load_spec(),
        args.pool,
        archive_path,
        args.source,
        args.limit,
        args.until,
        args.resume,
        enable_llm=args.llm,
        llm_endpoint=args.llm_endpoint,
        llm_deployment=args.llm_deployment,
        enable_llm_review=args.llm_review,
    )
    print(json.dumps({"master": relative(master_path), "pool_id": master["pool_id"], "until": args.until, "sources": len(master["artifacts"]["sources"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())