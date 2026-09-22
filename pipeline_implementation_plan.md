# Quiz Image Processing Pipeline

## Goal

Produce one hierarchical master JSON per input archive. The master JSON is the processing status and artifact description; it is not a database dump. Every stage records what it received, what it decided, what it created, quality signals, unresolved issues, and the exact handoff to the next stage.

The master JSON is authoritative for lineage and processing state. Images, OCR exports, contact sheets, and LLM responses remain separate files referenced by stable relative paths and content hashes. SQLite may be generated later as an optional query index, but it is never the source of truth.

## Verified input matrix

The current archives were inspected from their ZIP members, not inferred from filenames:

| Pool | ZIP members | File types | Decodable image format/mode | Dimensions | Layout/content families |
|---|---:|---|---|---|---|
| `original` | 31 | 31 `.jpg` | JPEG / RGB | 30 at `1275x1755`, 1 at `1148x1691` | 29 regular 3x3 card sheets, `hely1.jpg` mixed orientation/irregular layout, `tábla1.jpg` non-card board |
| `children` | 50 | 50 `.jpg` | JPEG / RGB | 50 at `1162x1600` | individual-card source images; layout still requires classification |

There are currently three processing layout families: regular card sheet, irregular or mixed-orientation collection, and individual card image. A fourth decision class, non-card content, is required because `tábla1.jpg` is an input image but must produce zero cards. There are no PNG, TIFF, BMP, WEBP, PDF, animated, grayscale, or corrupt image members in these two archives. The implementation still rejects unsupported types explicitly so a future archive cannot be silently misclassified.

## Durable output structure

The output root is `pipeline/`, separated by pool. Paths in the master JSON are relative to the repository root and every file reference carries a SHA-256 hash:

```text
pipeline/
  original/
    processing_master.json
    runs/<run_id>/run_manifest.json
    sources/<source_id>/original.jpg
    collections/source/source_collection_001.jpg
    collections/source/source_collection_001.json
    regions/<region_id>/proposal.json
    cards/<card_id>/card.jpg
    orientation/<card_id>/oriented.jpg
    ocr/<card_id>/ocr.json
    reviews/<review_id>/request.json
    reviews/<review_id>/response.json
  children/
    processing_master.json
    runs/<run_id>/run_manifest.json
    sources/<source_id>/original.jpg
    collections/source/source_collection_001.jpg
    collections/source/source_collection_001.json
```

The input ZIPs remain immutable outside `pipeline/<pool>/`. A stage writes to its run directory first and promotes complete files with an atomic rename. The master JSON is updated last. Temporary files, incomplete JSON, and logs never count as stage outputs.

## Automated phase gates

After each phase, the worker writes a checkpoint JSON report containing counts, artifact hashes, status totals, rejected/pending IDs, errors, and an evaluation summary. The worker does not wait for a person: it continues independent work, but an unsuccessful gate blocks only the dependent downstream stages and records them as `pending` with the blocking reason. A later `--resume` run reevaluates the gate from the same inputs and cache keys.

| Checkpoint | Review before continuing |
|---|---|
| Archive | Member count, suffix policy, archive hash, duplicate names, ZIP errors |
| Source files | Materialized paths, decode status, dimensions, mode, source hashes, extraction safety |
| Collections | Every source appears exactly once in a cell manifest; labels are readable; contact-sheet hash exists |
| Classification/layout | Every source has exactly one layout decision; board/non-card has zero accepted regions; uncertain items are pending |
| Fine tuning/extraction | Coordinates are in bounds; accepted regions do not overlap unexpectedly; card IDs remain linked to source hash and geometry |
| Orientation | Every accepted card has a transform and confidence; unknown orientation blocks OCR |
| OCR | Raw OCR exists; structured fields link to raw evidence; confidence and parse warnings are present |
| LLM review | Only flagged/pending items were sent; cache hits and new requests are counted; response schema passed |

The checkpoint is an automated audit surface. Decisions are produced only by deterministic validation, versioned configuration/rules, or the configured vision model. Any correction is an immutable derived decision record containing its producer, rule/model version, input hashes, confidence, and validation result; source artifacts are never edited in place.

## Partial-run modes

The pipeline must support these bounded runs:

```text
--until archive        # ZIP inventory only
--until sources        # inventory plus safe materialization/decode
--until collections    # adds labeled contact sheets and cell manifests
--until classification  # adds layout decisions; no crops, OCR, or LLM
--pool original|children
--source <member>      # restrict the probe to one ZIP member
--limit <n>            # deterministic first-n source members after natural sorting
--resume               # reuse valid stage outputs and continue pending descendants
```

The default exploratory run should be `--pool original --limit 3 --until classification`, followed by a second run on `hely1.jpg`, `tábla1.jpg`, and one child image. This exercises regular, irregular, non-card, and individual layouts before any expensive stage is enabled. A partial run is successful even when later stages are `pending`; it must never label unexecuted OCR or review as `available`.

## Stage contracts

| Stage | Required input | Required decisions | Required output | Gate for next stage |
|---|---|---|---|---|
| ZIP archive | Archive path and file hash | Is the archive readable? Which members are eligible images? | Archive descriptor with hash, member list, byte sizes, archive errors | Every eligible member has stable identity |
| Source files | Archive members | Can each image be opened? Dimensions, mode, hash, materialization path | One descriptor per source image | Every source file is addressable and decodable |
| Collection images | Source image descriptors | Which images belong in each automated evaluation collection? | Labeled contact sheets with member-to-cell map | Deterministic completeness checks pass and automated model evaluation can consume the collection |
| Page classification and basic layout estimation | Source images and collection evidence | `card_sheet_3x3`, `individual_card`, `mixed_orientation`, `non_card`, or `unknown`; estimate grid/regions | Per-source layout classification, geometry, confidence, evidence references | Only accepted regions proceed to fine tuning |
| Layout fine tuning | Classified source and estimated geometry | Crop bounds, excluded regions, rotation candidates, generated adjustments | Region proposals with coordinates, transform, confidence, decision provenance, and status | Every proposed card region is either accepted, rejected, or `model_review_pending` |
| Card extraction | Accepted region proposals | Is the crop a standalone card? Stable card identity and source linkage | Standalone card image plus crop provenance | No rejected/non-card region becomes a card |
| Orientation estimation | Standalone card images | Upright, clockwise, counter-clockwise, upside-down, mixed, or unknown | Orientation transform, confidence, and oriented image reference | OCR only sees accepted upright candidates or records pending orientation |
| OCR extraction | Oriented card images | Language, segmentation mode, text blocks, clue numbering, answer boundary, OCR confidence | Category, ordered clues, answer, raw OCR, line/word confidence, text quality flags | Structured text is complete enough for review or explicitly pending |
| LLM review and refinement | Only flagged/pending artifacts and their evidence | Visual validity, orientation, layout, OCR corrections, confidence; never invent missing text | Cached model-review result, accepted corrections, model metadata | Final record has status `verified`, `model_uncertain`, `rejected`, or `failed` |

## Required stage record

Every stage record must contain these fields, even when the stage is pending or failed:

- `stage_id`, `stage_name`, `implementation_version`, and `status`.
- `input.artifact_refs`, `input.required_information`, and `input.fingerprint`.
- `evaluation.method`, `evaluation.rules`, and `evaluation.decisions`.
- `outputs.artifact_refs`, `outputs.records`, and `outputs.fingerprint`.
- `quality.confidence`, `quality.warnings`, `quality.errors`, and `quality.review_required`.
- `handoff.accepted_refs`, `handoff.pending_refs`, `handoff.rejected_refs`, and `handoff.reason`.
- `cache.key`, `cache.parameters`, `cache.reused`, and `cache.source_stage_run`.
- `started_at_utc`, `completed_at_utc`, and `run_id` when execution was attempted.

An unavailable prerequisite is recorded as `pending` with an issue, not as `available`. A corrupt or non-card input is recorded as `rejected` with evidence. An item waiting for automated model evaluation is `model_review_pending`; a completed model evaluation that cannot decide is `model_uncertain`. `cached` means the prior output was reused unchanged, not that the stage was skipped without a prior successful result.

## Master JSON shape

```json
{
  "schema_version": "2.0",
  "artifact_type": "quiz_pool_processing_master",
  "pool_id": "original",
  "input": {},
  "processing": {
    "stage_order": [],
    "stages": [
      {
        "stage_id": "page_classification",
        "status": "available|cached|pending|model_review_pending|model_uncertain|rejected|failed",
        "implementation_version": "",
        "run_id": "",
        "cache": {"key": "", "parameters": {}, "reused": false, "source_stage_run": null},
        "input": {"artifact_refs": [], "required_information": [], "fingerprint": ""},
        "evaluation": {"method": "", "rules": [], "decisions": []},
        "outputs": {"artifact_refs": [], "records": [], "fingerprint": ""},
        "quality": {"confidence": null, "review_required": false, "errors": [], "warnings": []},
        "handoff": {"accepted_refs": [], "pending_refs": [], "rejected_refs": [], "reason": ""},
        "started_at_utc": null,
        "completed_at_utc": null
      }
    ]
  },
  "artifacts": {
    "sources": [],
    "regions": [],
    "cards": [],
    "reviews": []
  },
  "quality_summary": {}
}
```

## Iterative implementation plan

1. **Contract layer**: add `pipeline_spec.json` and validate every master JSON against the stage contract.
2. **Archive/source layer**: inventory ZIP members and materialized files with SHA-256 hashes; reuse records when hashes match.
3. **Visual collection layer**: generate contact sheets with a cell manifest, not just image filenames.
4. **Classification layer**: classify each source as card sheet, individual card, mixed orientation, non-card, or unknown; preserve evidence and confidence.
5. **Geometry layer**: represent crop proposals as coordinates and transforms. Do not apply a generic 3x3 crop to a source classified otherwise.
6. **Card layer**: emit standalone images only for accepted proposals. Keep rejected candidates linked in the master JSON.
7. **Orientation layer**: estimate orientation before OCR and record the transform used.
8. **OCR layer**: keep raw OCR, structured text, confidence, line coordinates, and parse warnings together.
9. **Model review layer**: send only `pending` or `model_review_pending` artifacts. Cache by image hash, prompt hash, model/deployment, and stage parameters.
10. **Finalization**: mark each card `verified`, `model_uncertain`, `rejected`, or `failed`; never overwrite verified text without a new model decision event.

## Idempotency rule

A stage may run only when its input artifact hash, parameters, and implementation version differ from the prior stage record. Otherwise it writes `status: cached` and preserves the previous outputs. A later-stage change invalidates only its descendants, not unrelated verified artifacts.

The cache key must include the content hash of every input artifact, not its path or modification time. For OCR it additionally includes language, Tesseract version, preprocessing, page segmentation mode, and parser version. For LLM review it includes image hash, prompt hash, deployment, API version, and review-schema version. Cache entries are immutable; a changed response is a new review event.

## Failure prevention and recovery

| Failure we already saw | Prevention in the new pipeline | Recovery behavior |
|---|---|---|
| Applying a 3x3 grid to `hely1.jpg`, the board image, or child cards | Classification is a mandatory gate. Geometry is selected per source; `individual_card` has no grid crop, `non_card` emits no regions, and `mixed_orientation` requires explicit regions/transforms. | Keep the source and rejected proposals in the master JSON; do not delete old evidence. |
| Generated card IDs becoming detached from source geometry | Card identity is derived from pool, source hash, region coordinates, and extraction version. Display sequence numbers are labels only. | Rebuild an affected descendant while preserving the prior record as a superseded artifact. |
| OCR text looking structured while containing wrong characters or crop errors | Store raw OCR, word/line confidence, parser warnings, answer-boundary evidence, and a deterministic model-review flag. No low-confidence parse is silently promoted to verified. | Mark `model_review_pending`; send only the card evidence to the configured model. |
| LLM output being malformed or progress text contaminating JSON | Store provider response and normalized review result separately; validate the result against a schema; write logs to stderr. | Keep the failed response, mark the review failed, and retry only with the same cache key after correction. |
| Repeated LLM calls and unnecessary OCR reruns | Content-hash cache keys plus immutable stage outputs; verified records are not submitted again unless the prompt/model/schema changes. | Reuse cached results and report `cached`; never infer cache validity from filenames. |
| Deleting `output/` and losing the only usable card export | Master runs are append-only and write temporary files followed by atomic rename. Existing imports are ingested as external artifacts before any regeneration. | A missing descendant becomes `pending`; source/archive stages remain usable. |
| A manifest or database summary hiding what was actually processed | Each stage records input refs, requirements, method, decisions, outputs, quality, and handoff in the same master document. | Partial runs remain inspectable and restart from the first invalid stage. |
| Original and child pools being mixed in one review/cache result | Pool ID, archive hash, source hash, and artifact path are required in every record and cache namespace. | Cross-pool references fail validation rather than being silently accepted. |

## What is possible now

- Inventory and hash both ZIP archives reproducibly.
- Materialize and describe all 31 original image members and all 50 child image members.
- Preserve the existing 279-card JSON as an imported legacy OCR artifact, without treating its images as valid if the image files are absent.
- Generate source contact sheets with a manifest mapping every cell back to a source hash.
- Classify original known exceptions deterministically and classify child files as individual-card candidates.
- Generate card crops only after classification and layout decisions.
- Run Hungarian OCR with raw evidence and structured parsing.
- Reuse existing LLM cache entries and isolate new reviews by content hash.

## What is still missing

- `master_pipeline.py` that creates and updates one master JSON per archive.
- A materialization stage that safely extracts archive members and records path traversal protection and decode errors.
- A contact-sheet manifest containing cell coordinates, source IDs, and image hashes.
- A layout classifier and region schema that handles sheets, individual cards, mixed orientation, and non-card images.
- Orientation estimation as a real stage rather than a targeted-candidate note.
- OCR provenance that includes engine/configuration versions and line/word confidence.
- Strict validation of stage records, artifact references, status transitions, and cross-pool boundaries.
- Atomic writes, run IDs, append-only review events, and recovery from interrupted runs.
- Focused tests for idempotency, board exclusion, irregular layout handling, child-image handling, malformed LLM responses, and missing descendants.
- Documentation that does not claim `pipeline.py` is the complete implementation until the master builder exists.

## Acceptance criteria before calling the pipeline complete

1. A clean run creates exactly one master JSON for each supplied archive and every stage has a valid record.
2. A second run with unchanged inputs performs no OCR or LLM request and marks reusable stages `cached`.
3. Deleting only a derived card image makes card extraction and descendants `pending` without invalidating archive/source records.
4. `tábla1.jpg` is `non_card` and produces zero accepted card regions.
5. `hely1.jpg` is `mixed_orientation`; its affected regions remain reviewable and are never silently treated as ordinary grid cells.
6. Child images are represented as individual-card sources and are not passed through a 3x3 crop.
7. Every final Hungarian quiz record contains its source/card/image provenance, raw OCR, structured text, review status, and unresolved warnings.
8. Invalid or incomplete LLM responses are retained as failed review events and cannot overwrite verified text.

## Current known exceptions

- `hely1.jpg`: mixed orientation and irregular bottom layout; generic 3x3 crops are not authoritative.
- `tábla1.jpg`: non-card board; all generated card-like crops are rejected.
- Original card 202: card crop exists but OCR requires targeted review.
- Child pool: 50 individual images require classification before extraction.
