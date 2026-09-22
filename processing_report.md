# Quiz Card Processing Report

Generated: `2026-09-22T18:39:55+00:00`

## Executive outcome

- Contract validation: **PASS**
- Processing masters found: **1** of 2 expected pools
- Pipeline stages defined: **9**
- Pipeline stage records attempted: **2**
- Current boundary: status below reflects the available pool master artifacts.

## KPI summary

| KPI | Value |
|---|---:|
| Pools with master JSON | 1 |
| Source artifacts | 3 |
| Region proposals | 0 |
| Card artifacts | 0 |
| Review artifacts | 0 |
| Unexecuted planned stages | 7 |
| Stage records present | 9 |
| Available stages | 2 |
| Cached stages | 0 |
| Pending stages | 7 |
| Model-review pending stages | 0 |
| Model-uncertain stages | 0 |
| Rejected stages | 0 |
| Failed stages | 0 |

## Stage status

| Phase | Stage ID | Status |
|---|---|---|
| ZIP archive | `zip_archive` | available: 1 |
| Source files | `source_files` | available: 1 |
| Collection images | `collection_images` | pending: 1 |
| Page classification and basic layout estimation | `page_classification` | pending: 1 |
| Layout fine tuning | `layout_fine_tuning` | pending: 1 |
| Card extraction | `card_extraction` | pending: 1 |
| Orientation estimation | `orientation_estimation` | pending: 1 |
| OCR extraction | `ocr_extraction` | pending: 1 |
| LLM review and refinement | `llm_review` | pending: 1 |

## Pool coverage

| Pool | Master artifact | Sources | Regions | Cards | Reviews |
|---|---|---:|---:|---:|---:|
| `original` | `pipeline/original/processing_master.json` | 3 | 0 | 0 | 0 |

## Contract review

- Schema version: `2.0`
- Artifact type: `quiz_pool_processing_master`
- Allowed statuses: `pending`, `available`, `cached`, `model_review_pending`, `model_uncertain`, `verified`, `rejected`, `failed`
- The specification and its status vocabulary are structurally valid.

## Next gate

Implement and run the `collection_images` phase for the selected pools, then regenerate this report. Do not interpret pending downstream stages as failures; they have not been executed yet.

## Source artifacts

- Specification: [pipeline_spec.json](pipeline_spec.json)
- Implementation plan: [pipeline_implementation_plan.md](pipeline_implementation_plan.md)
- Contract validator: [contract_validator.py](contract_validator.py)
