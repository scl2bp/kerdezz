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

The current inputs show several observed layout patterns: regular 3x3 collections, composite collections with a dominant vertical grid and a bottom pair of horizontal cards, irregular or mixed-orientation collections, and individual card images. These are observations, not routing classes. A separate `non_card` route is required because `tábla1.jpg` is an input image but must produce zero cards. There are no PNG, TIFF, BMP, WEBP, PDF, animated, grayscale, or corrupt image members in these two archives. The implementation still rejects unsupported types explicitly so a future archive cannot be silently misclassified.

## Classification policy

Classification produces a rich observation profile plus one automated routing decision. The routing vocabulary is intentionally small, but the observation profile is open-ended and must preserve all useful visual facts.

The routing classes are:

| Class | Routing meaning |
|---|---|
| `card_collection` | The source may contain multiple card regions; geometry comes from the observation profile, not from this route name. |
| `individual_card` | One source image is one card; no sheet grid is applied. |
| `non_card` | Readable input that is not quiz-card content; emits zero accepted regions. |
| `unknown` | Evidence is insufficient for safe routing; emits zero accepted regions and remains pending for dependent stages. |

The routing taxonomy is constrained only at the routing boundary. Each source also receives an `observation_profile` that may contain multiple content roles, such as `quiz_card_front`, `card_back`, and `game_board`, together with visual state, anomalies, feature summaries, candidate regions, and a layout pattern. The layout pattern may include `grid`, `single`, `composite_regions`, `irregular_regions`, `none`, or `unknown`, row and column counts, normalized row/column coefficients, gap coefficients, and region groups such as a dominant vertical grid plus a secondary horizontal pair. A source can therefore be routed as `unknown` while still retaining useful facts such as “probable game board” or “probable card back.”

Exactly one `routing_class` controls downstream geometry. `observation_profile` never silently promotes a source to a route, and model output never directly controls geometry. A new routing class requires a versioned contract change; a new visual variation or content role does not.

Every source receives an immutable classification decision record containing its source and collection hashes, cell reference, dimensions, candidate routing classes and scores, routing_class, observation_profile, confidence, rule/configuration versions, model deployment/prompt/response hashes when used, reason, implementation version, and replay key. This record is the input for deterministic reprocessing when a rule, configuration, model, or prompt changes.

### Layered classification execution

Classification is a staged evidence process, not one unrestricted model call:

1. **Source feature analysis** extracts deterministic signals from the source file: dimensions, aspect ratio, format/mode, borders, edge density, repeated separators, likely grid lines, and candidate region geometry. It creates a feature artifact keyed by the source hash and feature-rule version.
2. **Collection-level visual evaluation** sends the labeled contact sheet and its manifest to the configured vision model. The model compares sources in context and returns candidate classes, scores, visual attributes, and anomaly hints. It does not directly route geometry.
3. **First-level classification** deterministically fuses the feature artifact and collection evaluation. It emits exactly one `routing_class` or `unknown`, plus the rich `observation_profile`, candidate ranking, confidence, consistency checks, and exact replay key.
4. **Unknown-source evaluation** sends only unresolved sources to the vision model with their source image, feature evidence, collection context, and first-level decision. The result is diagnostic evidence: candidate route hints, content-role observations, layout coefficient suggestions, missing-feature suggestions, and alternative explanations. It cannot directly promote a route.
5. **Classification replay** runs the versioned deterministic fusion again with the diagnostic evidence. It writes a new immutable decision record and a decision delta. If the gates still do not pass, the source remains `unknown` and no regions are emitted.

This design constrains routing without constraining discovery. The routing vocabulary remains small and stable, while content roles, candidate subtypes, anomaly descriptions, layout coefficients, model hints, and new feature proposals are preserved as evidence. A future subtype can therefore be studied and reprocessed without being silently treated as a known geometry.

### Evidence required for later reprocessing

For every source, persist the source-feature JSON, collection-evaluation request and response, first-level decision JSON, and, when applicable, unknown-source request/response and replay decision. Each artifact records input hashes, producer, implementation/rule/configuration/model versions, prompt and response hashes, candidate scores, selected/unknown class, confidence, consistency checks, and the preceding decision record hash. Reprocessing loads these artifacts by hash and creates a new decision event; it never edits the original evidence.

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
| Classification/layout | Every source has exactly one current routing decision and rich observation profile; board/non-card observations have zero accepted regions; unknown items retain observations plus diagnostic and replay evidence |
| Fine tuning/extraction | Coordinates are in bounds; accepted regions do not overlap unexpectedly; card IDs remain linked to source hash and geometry |
| Orientation | Every accepted card has a transform and confidence; unknown orientation blocks OCR |
| OCR | Raw OCR exists; structured fields link to raw evidence; confidence and parse warnings are present |
| LLM review | Every quiz card is sent exactly once per review version; cache hits and new requests are counted; category and domain audits are present; trusted text corrections match existing OCR fields and do not invent new quiz content |
| Contract validation | Both pool masters, stage records, statuses, and referenced artifacts validate against the versioned specification |
| Processing report | The combined report is generated from validated masters and stored as a hashed terminal artifact |

The checkpoint is an automated audit surface. Decisions are produced only by deterministic validation, versioned configuration/rules, or the configured vision model. Any correction is an immutable derived decision record containing its producer, rule/model version, input hashes, confidence, and validation result; source artifacts are never edited in place.

## Partial-run modes

The pipeline must support these bounded runs:

```text
--until archive        # ZIP inventory only
--until sources        # inventory plus safe materialization/decode
--until collections    # adds labeled contact sheets and cell manifests
--until classification  # adds layout decisions; no crops, OCR, or LLM
--until fine_tuning     # adds validated crop proposals
--until card_extraction # adds standalone card images
--until orientation     # adds verified oriented card image artifacts
--until ocr            # adds Hungarian OCR and parsed quiz text
--until llm_review     # adds schema-validated review events and terminal card statuses
--pool original|children
--source <member>      # restrict the probe to one ZIP member
--limit <n>            # deterministic first-n source members after natural sorting
--resume               # reuse valid stage outputs and continue pending descendants
```

The default exploratory run should be `--pool original --limit 3 --until classification`, followed by a second run on `hely1.jpg`, `tábla1.jpg`, and one child image. This exercises regular, irregular, non-card, and individual layouts before any expensive stage is enabled. The full authoritative text run is `--pool original --until ocr`; it writes one orientation and one OCR artifact per accepted card. A partial run is successful even when later stages are `pending`; it must never label unexecuted OCR or review as `available`.

## Stage contracts

| Stage | Required input | Required decisions | Required output | Gate for next stage |
|---|---|---|---|---|
| ZIP archive | Archive path and file hash | Is the archive readable? Which members are eligible images? | Archive descriptor with hash, member list, byte sizes, archive errors | Every eligible member has stable identity |
| Source files | Archive members | Can each image be opened? Dimensions, mode, hash, materialization path | One descriptor per source image | Every source file is addressable and decodable |
| Collection images | Source image descriptors | Which images belong in each automated evaluation collection? | Labeled contact sheets with member-to-cell map | Deterministic completeness checks pass and automated model evaluation can consume the collection |
| Page classification and basic layout estimation | Source images and collection evidence | One routing class plus open-ended observation profile; estimate grid/regions only when safe | Per-source routing decision, content roles, visual state, layout coefficients, region candidates, confidence, evidence, and replay key | Only accepted regions proceed to fine tuning; `unknown` emits none but retains observations |
| Layout fine tuning | Classified source and estimated geometry | Crop bounds, excluded regions, rotation candidates, generated adjustments | Region proposals with coordinates, transform, confidence, decision provenance, and status | Every proposed card region is either accepted, rejected, or `model_review_pending` |
| Card extraction | Accepted region proposals | Is the crop a standalone card? Stable card identity and source linkage | Standalone card image plus crop provenance | No rejected/non-card region becomes a card |
| Orientation estimation | Standalone card images | Upright, clockwise, counter-clockwise, upside-down, mixed, or unknown | Orientation transform, confidence, and oriented image reference | OCR only sees accepted upright candidates or records pending orientation |
| OCR extraction | Oriented card images | Language, segmentation mode, text blocks, clue numbering, answer boundary, OCR confidence | Category, ordered clues, answer, raw OCR, line/word confidence, text quality flags | Structured text is complete enough for review or explicitly pending |
| LLM review and refinement | Every extracted quiz text and its OCR artifact provenance | Compact category/domain assessment and OCR review; keep valid text unchanged, use `corrected` for clear OCR errors, and use `modified` only for an intentional content change with a reason | One immutable compact review event per quiz card with `assessment`, complete `final_fields`, category assessment, and domain assessment | Final record has status `verified` or `failed` |
| Contract validation | All pool masters and the versioned specification | Schema, stage order, statuses, artifact references, final review statuses | Validation result JSON with master/spec hashes and errors | Every expected pool master is valid |
| Processing report | Validated pool masters and validation results | Pool coverage, stage completion, counts, pending/failed items, quality KPIs | `processing_report.md` plus SHA-256 artifact record | Final report is reproducible and linked from each master |

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
4. **Classification layer**: extract deterministic source features, evaluate labeled collections, fuse the evidence into one routing_class or `unknown` plus an observation profile, diagnose unknowns with source-level vision, and preserve immutable replay evidence.
5. **Geometry layer**: represent crop proposals as coordinates and transforms. Do not apply a generic 3x3 crop unless the observation profile independently supports that geometry.
6. **Card layer**: emit standalone images only for accepted proposals. Keep rejected candidates linked in the master JSON.
7. **Orientation layer**: estimate orientation before OCR and record the transform used.
8. **OCR layer**: keep raw OCR, structured text, confidence, line coordinates, and parse warnings together.
9. **Model review layer**: send every extracted quiz text individually with the same versioned prompt and schema. Require only compact category/domain assessments and complete `final_fields`; valid extracted text must remain `unchanged`, while clear OCR errors may be `corrected`. Cache by quiz-text hash, prompt/schema hash, model/deployment, and stage parameters.
10. **Post-OCR quality gate**: require `unchanged` to equal parsed OCR exactly, preserve raw OCR, and reject malformed responses. A `modified` result must include a reason; no evidence payload is required for an ordinary review.
11. **Contract validation**: validate every expected pool master and its final review statuses; write a validation result artifact with the specification and master hashes.
12. **Processing report**: generate the cross-pool report only after validation passes, record its SHA-256 as an artifact in each master, and regenerate once after those terminal records are committed.

## Idempotency rule

A stage may run only when its input artifact hash, parameters, and implementation version differ from the prior stage record. Otherwise it writes `status: cached` and preserves the previous outputs. A later-stage change invalidates only its descendants, not unrelated verified artifacts.

The cache key must include the content hash of every input artifact, not its path or modification time. For OCR it additionally includes language, Tesseract version, preprocessing, page segmentation mode, and parser version. For LLM review it includes the extracted quiz-text hash, prompt hash, deployment, API version, and review-schema version. Cache entries are immutable; a changed response is a new review event.

## Failure prevention and recovery

| Failure we already saw | Prevention in the new pipeline | Recovery behavior |
|---|---|---|
| Applying a 3x3 grid to `hely1.jpg`, the board image, or child cards | Classification is a mandatory gate. Geometry is selected per source; `individual_card` has no collection grid, `non_card` emits no regions, and a `card_collection` with `mixed_orientation` observations requires explicit regions/transforms. | Keep the source and rejected proposals in the master JSON; do not delete old evidence. |
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
- Run the LLM review phase for every quiz card and preserve immutable OCR corrections plus category/domain audit findings.
- Validate both pool masters and publish the combined processing report as a hashed terminal artifact.
- Reuse existing LLM cache entries and isolate new reviews by content hash.

## What is still missing

- Deterministic cache reuse across repeated full runs; the current runner records cache keys but still rebuilds stages rather than reusing prior descendants.
- Stronger status-transition and per-artifact SHA-256 validation beyond the terminal finalizer's reference checks.
- Production LLM execution against the configured Azure deployment, including live response and retry monitoring.
- Full idempotency and missing-descendant recovery tests for every stage.

## Acceptance criteria before calling the pipeline complete

1. A clean run creates exactly one master JSON for each supplied archive and every stage has a valid record.
2. A second run with unchanged inputs performs no OCR or LLM request and marks reusable stages `cached`.
3. Deleting only a derived card image makes card extraction and descendants `pending` without invalidating archive/source records.
4. `tábla1.jpg` is `non_card` and produces zero accepted card regions.
5. `hely1.jpg` routes as `card_collection` only if its observations support card regions; its `mixed_orientation` and irregular-layout observations remain reviewable and are never silently treated as ordinary grid cells.
6. Child images are represented as individual-card sources and are not passed through a 3x3 crop.
7. Every final Hungarian quiz record contains its source/card/image provenance, raw OCR, structured text, review status, and unresolved warnings.
8. Invalid or incomplete LLM responses are retained as failed review events and cannot overwrite verified text.

## Current known exceptions

- `hely1.jpg`: mixed orientation and irregular bottom layout; generic 3x3 crops are not authoritative.
- `tábla1.jpg`: non-card board; all generated card-like crops are rejected.
- Original card 202: card crop exists but OCR requires targeted review.
- Child pool: 50 individual images require classification before extraction.

## Post-OCR quality plan

OCR is an extraction result, not a final truth claim. Before a card can be marked `verified`, the LLM review stage and deterministic finalization gate should cover these checks:

1. **Crop integrity**: the card image matches the source hash, accepted region, geometry decision, and extraction transform; no border or neighboring-card contamination is silently accepted.
2. **Orientation**: text is evaluated in the recorded upright orientation, with mixed or ambiguous orientation remaining review-pending.
3. **Text evidence**: raw OCR, line/word coordinates, confidence, parser warnings, and answer-boundary evidence remain attached to every proposed correction.
4. **Semantic structure**: category, ordered clues, and answer are checked for plausible field boundaries and card completeness. The model may repair recognition errors but must not invent absent text.
5. **Text-review agreement**: every accepted correction must identify the corrected field, exactly match the OCR old value, and include the model's language/context evidence, confidence, and reason.
6. **Hungarian character fidelity**: treat missing final letters and likely confusions such as `Ő/O/Ö/Ó`, `Ű/U/Ü/Ú`, `E/É`, `A/Á`, and `I/Í` as explicit correction hypotheses. Preserve the exact Unicode character selected by Luna; never normalize diacritics away.
7. **Spelling correction discipline**: a spelling change is accepted when Luna's trusted language/context review returns a valid correction whose old value matches the extracted OCR field. The model must not invent a name, answer, clue, or fact.
8. **Schema and cache safety**: malformed, incomplete, stale, or cross-pool responses become failed review events; unchanged verified images are not sent again.
9. **Terminal status**: each card ends as `verified`, `model_uncertain`, `rejected`, or `failed`, with unresolved warnings retained. Only then can contract validation and the report stage run.
