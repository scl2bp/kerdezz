"""Review flagged OCR records with evidence-backed Hungarian text correction."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


IMPLEMENTATION_VERSION = "llm-review-v1"
PROMPT_SCHEMA_VERSION = "hungarian-card-review-v1"
REVIEW_THRESHOLD = 0.75
TERMINAL_STATUSES = {"verified", "model_uncertain", "rejected", "failed"}
CORRECTION_FIELDS = {"category", "answer", "clues"}


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


def _needs_review(record: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if record.get("status") != "available":
        reasons.append(f"ocr_status:{record.get('status', 'missing')}")
    if record.get("parse_status") != "complete":
        reasons.append(f"parse_status:{record.get('parse_status', 'missing')}")
    if not record.get("raw_text"):
        reasons.append("empty_raw_text")
    if float(record.get("confidence", 0.0) or 0.0) < REVIEW_THRESHOLD:
        reasons.append("low_ocr_confidence")
    warnings = record.get("warnings", [])
    if isinstance(warnings, list):
        reasons.extend(f"ocr_warning:{warning}" for warning in warnings)
    return bool(reasons), reasons


def build_review_queue(ocr_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for record in ocr_records:
        needs_review, reasons = _needs_review(record)
        if needs_review:
            queue.append(
                {
                    "card_id": record.get("card_id"),
                    "ocr_artifact_ref": record.get("artifact_ref"),
                    "oriented_image": record.get("oriented_image"),
                    "reasons": reasons,
                }
            )
    return queue


def review_prompt(ocr: dict[str, Any]) -> str:
    evidence = {
        "card_id": ocr.get("card_id"),
        "raw_text": ocr.get("raw_text", ""),
        "lines": ocr.get("lines", []),
        "parsed": ocr.get("parsed", {}),
        "confidence": ocr.get("confidence"),
        "warnings": ocr.get("warnings", []),
    }
    return (
        "Review this Hungarian quiz card image and the OCR evidence. Return JSON only. "
        "Correct OCR only when the pixels support the correction. Check clipped final characters, "
        "spelling substitutions, and Hungarian diacritics including ő/ö/ó, ű/ü/ú, é, á, and í. "
        "Do not use linguistic plausibility alone and do not invent text that is not visible. "
        "Set no_invention to true only when every accepted value is visibly supported. "
        "Schema: {decision: verified|corrected|model_uncertain|rejected|failed, confidence: number 0..1, "
        "no_invention: boolean, reason: string, corrections: [{field: category|answer|clues, "
        "old_value: string, new_value: string, evidence: [string], confidence: number}]}.\n\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2)
    )


def _content_from_completion(completion: Any) -> str:
    choices = getattr(completion, "choices", None) or []
    if not choices:
        raise ValueError("model response contains no choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        if text:
            return text
    raise ValueError("model response contains no text content")


def azure_review(image_path: Path, prompt: str, endpoint: str, deployment: str) -> dict[str, Any]:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    )
    client = AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2025-01-01-preview",
    )
    encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
    completion = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "developer", "content": "You review Hungarian OCR conservatively and return JSON only."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}},
                ],
            },
        ],
        max_completion_tokens=3000,
        response_format={"type": "json_object"},
        stream=False,
    )
    return json.loads(_content_from_completion(completion))


def validate_response(response: Any) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(response, dict):
        return None, ["response must be an object"]
    decision = response.get("decision")
    if decision not in TERMINAL_STATUSES | {"corrected"}:
        errors.append("decision must be verified, corrected, model_uncertain, rejected, or failed")
    confidence = response.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        errors.append("confidence must be a number from 0 to 1")
    if response.get("no_invention") is not True:
        errors.append("no_invention must be true")
    if not isinstance(response.get("reason"), str) or not response["reason"].strip():
        errors.append("reason must be a non-empty string")
    corrections = response.get("corrections", [])
    if not isinstance(corrections, list):
        errors.append("corrections must be a list")
        corrections = []
    for index, correction in enumerate(corrections):
        prefix = f"corrections[{index}]"
        if not isinstance(correction, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if correction.get("field") not in CORRECTION_FIELDS:
            errors.append(f"{prefix}.field is unsupported")
        for field in ("old_value", "new_value"):
            if not isinstance(correction.get(field), str) or not correction[field].strip():
                errors.append(f"{prefix}.{field} must be a non-empty string")
        evidence = correction.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(item, str) and item.strip() for item in evidence):
            errors.append(f"{prefix}.evidence must contain visible-evidence strings")
        if not isinstance(correction.get("confidence"), (int, float)) or not 0 <= float(correction["confidence"]) <= 1:
            errors.append(f"{prefix}.confidence must be a number from 0 to 1")
    if decision == "corrected" and not corrections:
        errors.append("corrected decision requires at least one correction")
    if decision != "corrected" and corrections:
        errors.append("corrections are allowed only with corrected decision")
    return (response, errors) if not errors else (None, errors)


def _apply_corrections(ocr: dict[str, Any], corrections: list[dict[str, Any]]) -> dict[str, Any]:
    parsed = json.loads(json.dumps(ocr.get("parsed", {}), ensure_ascii=False))
    for correction in corrections:
        field = correction["field"]
        if field == "clues":
            clues = parsed.get("clues", [])
            if not isinstance(clues, list) or correction["old_value"] not in clues:
                raise ValueError(f"clue correction does not match OCR value: {correction['old_value']!r}")
            parsed["clues"] = [correction["new_value"] if clue == correction["old_value"] else clue for clue in clues]
        else:
            if parsed.get(field) != correction["old_value"]:
                raise ValueError(f"{field} correction does not match OCR value: {correction['old_value']!r}")
            parsed[field] = correction["new_value"]
    return parsed


def review_records(
    ocr_records: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    root: Path,
    pool_root: Path,
    *,
    enable_model: bool,
    endpoint: str | None,
    deployment: str | None,
    review_fn: Callable[[Path, str, str, str], dict[str, Any]] = azure_review,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str], dict[str, Any]]:
    ocr_by_card = {str(item.get("card_id")): item for item in ocr_records}
    cards_by_id = {str(item.get("card_id")): item for item in cards}
    queue = build_review_queue(ocr_records)
    reviews: list[dict[str, Any]] = []
    artifacts: list[dict[str, str]] = []
    errors: list[str] = []
    cache_path = pool_root / "reviews" / "llm_review_cache.json"
    cache: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            value = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                cache = value
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"review cache unreadable: {error}")
    for card in cards:
        card["final_status"] = "verified" if str(card.get("card_id")) not in {str(item.get("card_id")) for item in queue} else "model_review_pending"
    for item in queue:
        card_id = str(item.get("card_id"))
        ocr = ocr_by_card.get(card_id)
        card = cards_by_id.get(card_id)
        if not ocr or not card:
            errors.append(f"{card_id}: review input is incomplete")
            continue
        image_ref = ocr.get("oriented_image") or card.get("image_ref") or {}
        image_path = root / str(image_ref.get("path", ""))
        if not image_path.is_file():
            errors.append(f"{card_id}: review image is missing")
            continue
        image_sha256 = str(image_ref.get("sha256") or sha256_file(image_path))
        prompt = review_prompt(ocr)
        cache_key = object_hash({"image_sha256": image_sha256, "prompt": prompt, "deployment": deployment, "schema": PROMPT_SCHEMA_VERSION, "implementation_version": IMPLEMENTATION_VERSION})
        review_id = f"review_{card_id}_{cache_key[:16]}"
        review_dir = pool_root / "reviews" / review_id
        request = {"artifact_type": "llm_review_request", "review_id": review_id, "card_id": card_id, "image": {"path": relative(image_path, root), "sha256": image_sha256}, "ocr_artifact_ref": ocr.get("artifact_ref"), "prompt_schema": PROMPT_SCHEMA_VERSION, "prompt": prompt, "cache_key": cache_key, "created_at_utc": utc_now()}
        request_hash = atomic_json(review_dir / "request.json", request)
        artifacts.append({"path": relative(review_dir / "request.json", root), "sha256": request_hash})
        response: Any = cache.get(cache_key)
        source = "cache"
        if response is None:
            if not enable_model or not endpoint or not deployment:
                continue
            source = "model"
            try:
                response = review_fn(image_path, prompt, endpoint, deployment)
            except Exception as error:
                errors.append(f"{card_id}: model review failed: {error}")
                response = {"decision": "failed", "confidence": 0.0, "no_invention": True, "reason": str(error), "corrections": []}
            cache[cache_key] = response
            atomic_json(cache_path, cache)
        normalized, validation_errors = validate_response(response)
        final_fields: dict[str, Any] | None = None
        if normalized and not validation_errors and normalized["decision"] == "corrected":
            try:
                final_fields = _apply_corrections(ocr, normalized["corrections"])
            except ValueError as error:
                validation_errors.append(str(error))
        if validation_errors:
            errors.extend(f"{card_id}: {error}" for error in validation_errors)
            normalized = {"decision": "failed", "confidence": 0.0, "no_invention": True, "reason": "; ".join(validation_errors), "corrections": []}
        decision = str(normalized["decision"])
        final_status = "verified" if decision in {"verified", "corrected"} else decision
        card["final_status"] = final_status
        card["review_status"] = final_status
        review = {"artifact_type": "llm_review_event", "implementation_version": IMPLEMENTATION_VERSION, "review_id": review_id, "card_id": card_id, "status": final_status, "decision": decision, "source": source, "cache_key": cache_key, "request_ref": {"path": relative(review_dir / "request.json", root), "sha256": request_hash}, "response": normalized, "accepted_fields": normalized.get("corrections", []), "created_at_utc": utc_now()}
        review["final_fields"] = final_fields or ocr.get("parsed", {})
        response_hash = atomic_json(review_dir / "response.json", review)
        review["artifact_ref"] = {"path": relative(review_dir / "response.json", root), "sha256": response_hash}
        reviews.append(review)
        artifacts.append(review["artifact_ref"])
        card["review_ref"] = review["artifact_ref"]
    if cache:
        atomic_json(cache_path, cache)
    pending = sum(card.get("final_status") == "model_review_pending" for card in cards)
    summary = {"queue_count": len(queue), "reviewed_count": len(reviews), "pending_count": pending, "verified_count": sum(card.get("final_status") == "verified" for card in cards), "uncertain_count": sum(card.get("final_status") == "model_uncertain" for card in cards), "failed_count": sum(card.get("final_status") == "failed" for card in cards)}
    return reviews, artifacts, errors, summary
