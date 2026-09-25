"""Review every extracted Hungarian quiz text with trusted language-model refinement."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


IMPLEMENTATION_VERSION = "llm-review-v6"
PROMPT_SCHEMA_VERSION = "hungarian-quiz-text-review-v5"
REVIEW_SCOPE = "all_quiz_cards"
REVIEW_THRESHOLD = 0.75
TERMINAL_STATUSES = {"verified", "model_uncertain", "rejected", "failed"}
DEFAULT_ENDPOINT = "https://ae-oa-d-we-004.openai.azure.com/"
DEFAULT_DEPLOYMENT = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "medium"
REASONING_EFFORTS = {"low", "medium"}
NOISE_TOKENS = {"!", "]", "[", "j", "i", "he", "gi", "rtl", "meleg"}


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
    low_confidence_noise: list[str] = []
    for line in record.get("lines", []):
        if not isinstance(line, dict):
            continue
        for word in line.get("words", []):
            if not isinstance(word, dict):
                continue
            text = str(word.get("text", "")).strip()
            confidence = float(word.get("confidence", 100.0) or 0.0)
            if text.lower() in NOISE_TOKENS and confidence < 70:
                low_confidence_noise.append(text)
    if low_confidence_noise:
        reasons.append("low_confidence_noise:" + ",".join(sorted(set(low_confidence_noise))))
    return bool(reasons), reasons


def build_review_queue(ocr_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for record in ocr_records:
        _, diagnostic_reasons = _needs_review(record)
        queue.append(
            {
                "card_id": record.get("card_id"),
                "ocr_artifact_ref": record.get("artifact_ref"),
                "review_scope": REVIEW_SCOPE,
                "reasons": ["full_quiz_review", *diagnostic_reasons],
            }
        )
    return queue


def review_prompt(ocr: dict[str, Any]) -> str:
    quiz_text = {
        "card_id": ocr.get("card_id"),
        "category": ocr.get("parsed", {}).get("category", ""),
        "clues": ocr.get("parsed", {}).get("clues", []),
        "answer": ocr.get("parsed", {}).get("answer", ""),
    }
    return (
        "Review this Hungarian quiz card's extracted text. Return only compact JSON. "
        "Keep text unchanged when it is already correct. Use assessment=corrected only for a clear OCR error; "
        "use assessment=modified only for a real content change, never for stylistic or grammatical preference. "
        "Do not invent clues, answers, or facts. Review the category and domain, but do not rewrite content because "
        "of that review. The final_fields object must contain the complete category, clues, and answer. "
        "reason is required only for modified and must be omitted otherwise. "
        "Schema: {assessment: unchanged|corrected|modified, category_assessment: confirmed|uncertain|incorrect, "
        "domain_assessment: consistent|uncertain|issue, final_fields: {category: string, clues: [string], "
        "answer: string}, reason: string only when assessment=modified}.\n\n"
        + json.dumps(quiz_text, ensure_ascii=False, indent=2)
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


def azure_review(prompt: str, endpoint: str, deployment: str, reasoning_effort: str) -> dict[str, Any]:
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
    completion = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "developer", "content": "You review Hungarian quiz OCR as a trusted language editor and return JSON only."},
            {"role": "user", "content": prompt},
        ],
        max_completion_tokens=3000,
        reasoning_effort=reasoning_effort,
        response_format={"type": "json_object"},
        stream=False,
    )
    return json.loads(_content_from_completion(completion))


def validate_response(response: Any) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(response, dict):
        return None, ["response must be an object"]
    assessment = response.get("assessment")
    if assessment not in {"unchanged", "corrected", "modified"}:
        errors.append("assessment must be unchanged, corrected, or modified")
    if response.get("category_assessment") not in {"confirmed", "uncertain", "incorrect"}:
        errors.append("category_assessment is invalid")
    if response.get("domain_assessment") not in {"consistent", "uncertain", "issue"}:
        errors.append("domain_assessment is invalid")
    final_fields = response.get("final_fields")
    if not isinstance(final_fields, dict):
        errors.append("final_fields must be an object")
        final_fields = {}
    if not isinstance(final_fields.get("category"), str):
        errors.append("final_fields.category must be a string")
    if not isinstance(final_fields.get("clues"), list) or not all(isinstance(item, str) for item in final_fields.get("clues", [])):
        errors.append("final_fields.clues must be a list of strings")
    if not isinstance(final_fields.get("answer"), str):
        errors.append("final_fields.answer must be a string")
    reason = response.get("reason")
    if assessment == "modified" and (not isinstance(reason, str) or not reason.strip()):
        errors.append("reason is required for modified assessment")
    if assessment != "modified" and "reason" in response:
        errors.append("reason must be omitted unless assessment is modified")
    return (response, errors) if not errors else (None, errors)


def review_records(
    ocr_records: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    root: Path,
    pool_root: Path,
    *,
    enable_model: bool,
    endpoint: str | None,
    deployment: str | None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    review_fn: Callable[[str, str, str, str], dict[str, Any]] = azure_review,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str], dict[str, Any]]:
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(f"reasoning_effort must be one of {sorted(REASONING_EFFORTS)}")
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
    queued_card_ids = {str(item.get("card_id")) for item in queue}
    for card in cards:
        card["final_status"] = "model_review_pending" if str(card.get("card_id")) in queued_card_ids else "rejected"
    for item in queue:
        card_id = str(item.get("card_id"))
        ocr = ocr_by_card.get(card_id)
        card = cards_by_id.get(card_id)
        if not ocr or not card:
            errors.append(f"{card_id}: review input is incomplete")
            continue
        prompt = review_prompt(ocr)
        cache_key = object_hash({"quiz_text": ocr.get("parsed", {}), "prompt": prompt, "deployment": deployment, "reasoning_effort": reasoning_effort, "schema": PROMPT_SCHEMA_VERSION, "implementation_version": IMPLEMENTATION_VERSION})
        review_id = f"review_{card_id}_{cache_key[:16]}"
        review_dir = pool_root / "reviews" / review_id
        request = {"artifact_type": "llm_review_request", "review_id": review_id, "card_id": card_id, "review_scope": REVIEW_SCOPE, "ocr_artifact_ref": ocr.get("artifact_ref"), "quiz_text": ocr.get("parsed", {}), "prompt_schema": PROMPT_SCHEMA_VERSION, "reasoning_effort": reasoning_effort, "prompt": prompt, "cache_key": cache_key, "created_at_utc": utc_now()}
        request_hash = atomic_json(review_dir / "request.json", request)
        artifacts.append({"path": relative(review_dir / "request.json", root), "sha256": request_hash})
        response: Any = cache.get(cache_key)
        source = "cache"
        if response is None:
            if not enable_model or not endpoint or not deployment:
                continue
            source = "model"
            try:
                response = review_fn(prompt, endpoint, deployment, reasoning_effort)
            except Exception as error:
                errors.append(f"{card_id}: model review failed: {error}")
                response = {"assessment": "unchanged", "category_assessment": "uncertain", "domain_assessment": "uncertain", "final_fields": ocr.get("parsed", {}), "error": str(error)}
            cache[cache_key] = response
            atomic_json(cache_path, cache)
        normalized, validation_errors = validate_response(response)
        final_fields: dict[str, Any] | None = None
        if normalized and not validation_errors:
            final_fields = normalized["final_fields"]
            ocr_fields = ocr.get("parsed", {})
            if normalized["assessment"] == "unchanged" and final_fields != ocr_fields:
                validation_errors.append("unchanged assessment requires final_fields to equal OCR fields")
            if normalized["assessment"] == "corrected" and final_fields == ocr_fields:
                validation_errors.append("corrected assessment requires at least one changed field")
        if validation_errors:
            errors.extend(f"{card_id}: {error}" for error in validation_errors)
            normalized = {"assessment": "unchanged", "category_assessment": "uncertain", "domain_assessment": "uncertain", "final_fields": ocr.get("parsed", {}), "error": "; ".join(validation_errors)}
            final_fields = normalized["final_fields"]
        assessment = str(normalized["assessment"])
        final_status = "verified" if not validation_errors else "failed"
        card["final_status"] = final_status
        card["review_status"] = final_status
        review = {"artifact_type": "llm_review_event", "implementation_version": IMPLEMENTATION_VERSION, "review_id": review_id, "card_id": card_id, "review_scope": REVIEW_SCOPE, "status": final_status, "assessment": assessment, "source": source, "cache_key": cache_key, "request_ref": {"path": relative(review_dir / "request.json", root), "sha256": request_hash}, "response": normalized, "final_fields": final_fields or ocr.get("parsed", {}), "created_at_utc": utc_now()}
        response_hash = atomic_json(review_dir / "response.json", review)
        review["artifact_ref"] = {"path": relative(review_dir / "response.json", root), "sha256": response_hash}
        reviews.append(review)
        artifacts.append(review["artifact_ref"])
        card["review_ref"] = review["artifact_ref"]
        card["final_fields"] = review["final_fields"]
    if cache:
        atomic_json(cache_path, cache)
    pending = sum(card.get("final_status") == "model_review_pending" for card in cards)
    summary = {"queue_count": len(queue), "reviewed_count": len(reviews), "pending_count": pending, "verified_count": sum(card.get("final_status") == "verified" for card in cards), "uncertain_count": sum(card.get("final_status") == "model_uncertain" for card in cards), "failed_count": sum(card.get("final_status") == "failed" for card in cards)}
    return reviews, artifacts, errors, summary
