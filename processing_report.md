# Quiz Card Processing Report

Generated: `2026-09-23T07:07:41+00:00`

## Executive outcome

- Contract validation: **PASS**
- Processing masters found: **1** of 2 expected pools
- Pipeline stages defined: **9**
- Pipeline stage records attempted: **6**
- Current boundary: status below reflects the available pool master artifacts.

## KPI summary

| KPI | Value |
|---|---:|
| Pools with master JSON | 1 |
| Source artifacts | 31 |
| Region proposals | 231 |
| Card artifacts | 231 |
| Review artifacts | 0 |
| Unexecuted planned stages | 3 |
| Stage records present | 9 |
| Available stages | 6 |
| Cached stages | 0 |
| Pending stages | 3 |
| Model-review pending stages | 0 |
| Model-uncertain stages | 0 |
| Rejected stages | 0 |
| Failed stages | 0 |

## Stage status

| Phase | Stage ID | Status | Scope |
|---|---|---|---|
| ZIP archive | `zip_archive` | available: 1 | 31 members, 31 selected |
| Source files | `source_files` | available: 1 | 31 sources |
| Collection images | `collection_images` | available: 1 | 31 source cells |
| Page classification and basic layout estimation | `page_classification` | available: 1 | 31 sources, 31 feature artifacts, 231 accepted regions |
| Layout fine tuning | `layout_fine_tuning` | available: 1 | 231 proposals |
| Card extraction | `card_extraction` | available: 1 | 231 cards |
| Orientation estimation | `orientation_estimation` | pending: 1 |  |
| OCR extraction | `ocr_extraction` | pending: 1 |  |
| LLM review and refinement | `llm_review` | pending: 1 |  |

## Pool coverage

| Pool | Master artifact | Sources | Regions | Cards | Reviews |
|---|---|---:|---:|---:|---:|
| `original` | `pipeline/original/processing_master.json` | 31 | 231 | 231 | 0 |

## Contract review

- Schema version: `2.0`
- Artifact type: `quiz_pool_processing_master`
- Allowed statuses: `pending`, `available`, `cached`, `model_review_pending`, `model_uncertain`, `verified`, `rejected`, `failed`
- The specification and its status vocabulary are structurally valid.

## Next gate

Implement and run the `orientation_estimation` phase for the selected pools, then regenerate this report. Do not interpret pending downstream stages as failures; they have not been executed yet.

## Source artifacts

- Specification: [pipeline_spec.json](pipeline_spec.json)
- Implementation plan: [pipeline_implementation_plan.md](pipeline_implementation_plan.md)
- Contract validator: [contract_validator.py](contract_validator.py)
