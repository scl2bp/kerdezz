# Quiz Card Processing Report

Generated: `2026-09-22T18:24:25+00:00`

## Executive outcome

- Contract validation: **PASS**
- Processing masters found: **0** of 2 expected pools
- Pipeline stages defined: **9**
- Pipeline stage records executed: **0**
- Current boundary: the contract layer is validated; archive processing and downstream stages are not yet executed.

## KPI summary

| KPI | Value |
|---|---:|
| Pools with master JSON | 0 |
| Source artifacts | 0 |
| Region proposals | 0 |
| Card artifacts | 0 |
| Review artifacts | 0 |
| Available stages | 0 |
| Cached stages | 0 |
| Pending stages | 0 |
| Model-review pending stages | 0 |
| Model-uncertain stages | 0 |
| Rejected stages | 0 |
| Failed stages | 0 |

## Stage status

| Phase | Stage ID | Status |
|---|---|---|
| ZIP archive | `zip_archive` | pending (not executed) |
| Source files | `source_files` | pending (not executed) |
| Collection images | `collection_images` | pending (not executed) |
| Page classification and basic layout estimation | `page_classification` | pending (not executed) |
| Layout fine tuning | `layout_fine_tuning` | pending (not executed) |
| Card extraction | `card_extraction` | pending (not executed) |
| Orientation estimation | `orientation_estimation` | pending (not executed) |
| OCR extraction | `ocr_extraction` | pending (not executed) |
| LLM review and refinement | `llm_review` | pending (not executed) |

## Pool coverage

No `pipeline/<pool>/processing_master.json` exists yet. This is expected before archive/source execution.

## Contract review

- Schema version: `2.0`
- Artifact type: `quiz_pool_processing_master`
- Allowed statuses: `pending`, `available`, `cached`, `model_review_pending`, `model_uncertain`, `verified`, `rejected`, `failed`
- The specification and its status vocabulary are structurally valid.

## Next gate

Run the archive/source phase for a bounded probe (`original`, first 3 members), then regenerate this report. Do not interpret pending downstream stages as failures; they have not been executed yet.

## Source artifacts

- Specification: [pipeline_spec.json](pipeline_spec.json)
- Implementation plan: [pipeline_implementation_plan.md](pipeline_implementation_plan.md)
- Contract validator: [contract_validator.py](contract_validator.py)
