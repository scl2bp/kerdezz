# Quiz Card Processing Report

Generated: `2026-09-25T10:31:43+00:00`

## Executive outcome

- Contract validation: **PASS**
- Processing masters found: **2** of 2 expected pools
- Pipeline stages defined: **9**
- Pipeline stage records attempted: **16**
- Current boundary: status below reflects the available pool master artifacts.

## KPI summary

| KPI | Value |
|---|---:|
| Pools with master JSON | 2 |
| Source artifacts | 81 |
| Region proposals | 600 |
| Card artifacts | 600 |
| Review artifacts | 0 |
| Unexecuted planned stages | 2 |
| Stage records present | 18 |
| Available stages | 16 |
| Cached stages | 0 |
| Pending stages | 2 |
| Model-review pending stages | 0 |
| Model-uncertain stages | 0 |
| Rejected stages | 0 |
| Failed stages | 0 |

## Stage status

| Phase | Stage ID | Status | Scope |
|---|---|---|---|
| ZIP archive | `zip_archive` | available: 2 | 81 members, 81 selected |
| Source files | `source_files` | available: 2 | 81 sources |
| Collection images | `collection_images` | available: 2 | 81 source cells |
| Page classification and basic layout estimation | `page_classification` | available: 2 | 81 sources, 81 feature artifacts, 600 accepted regions |
| Layout fine tuning | `layout_fine_tuning` | available: 2 | 600 proposals |
| Card extraction | `card_extraction` | available: 2 | 600 cards |
| Orientation estimation | `orientation_estimation` | available: 2 | 600 oriented cards |
| OCR extraction | `ocr_extraction` | available: 2 | 600 OCR records |
| LLM review and refinement | `llm_review` | pending: 2 |  |

## Pool coverage

| Pool | Master artifact | Sources | Regions | Cards | Reviews |
|---|---|---:|---:|---:|---:|
| `children` | `pipeline/children/processing_master.json` | 50 | 369 | 369 | 0 |
| `original` | `pipeline/original/processing_master.json` | 31 | 231 | 231 | 0 |

## Contract review

- Schema version: `2.0`
- Artifact type: `quiz_pool_processing_master`
- Allowed statuses: `pending`, `available`, `cached`, `model_review_pending`, `model_uncertain`, `verified`, `rejected`, `failed`
- The specification and its status vocabulary are structurally valid.

## Next gate

Implement and run the `llm_review` phase for the selected pools, then regenerate this report. Do not interpret pending downstream stages as failures; they have not been executed yet.

## Source artifacts

- Specification: [pipeline_spec.json](pipeline_spec.json)
- Implementation plan: [pipeline_implementation_plan.md](pipeline_implementation_plan.md)
- Contract validator: [contract_validator.py](contract_validator.py)
