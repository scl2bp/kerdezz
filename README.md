# Kerdezz card extraction and processing pipeline

This project classifies Hungarian quiz-card sheets, extracts standalone card images, estimates orientation, and stores authoritative OCR evidence. The supplied original archive contains 31 source images and currently produces 231 accepted cards after geometry validation.

## Prerequisites

Install Tesseract and its Hungarian language data:

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-hun
python -m pip install -r requirements.txt
```

## Legacy extraction

```bash
python extract_cards.py KerdezzFelelek_ALAP.zip --output output
```

The command creates:

- `output/cards/card_001.jpg` through `card_279.jpg`
- `output/cards.json`, containing the source location, image path, OCR category, clues, answer, and raw OCR text for every card

The raw OCR is kept because the source scans contain occasional hyphenated line breaks and characters that may need manual review.

The legacy command is retained for comparison. Its `cards.json` is not the authoritative output of the current content-addressed pipeline.

## Authoritative pipeline

Run the current master pipeline through OCR with:

```bash
python master_pipeline.py --pool original --until ocr
```

The master JSON is written to `pipeline/original/processing_master.json`. It links each accepted card to:

- `pipeline/original/cards/<card_id>/card.jpg`
- `pipeline/original/orientation/<card_id>/oriented.jpg` and `orientation.json`
- `pipeline/original/ocr/<card_id>/ocr.json`

OCR records preserve raw text, ordered lines and words, parsed category/clues/answer fields, confidence, warnings, Tesseract configuration, and source/card/orientation hashes. Low-confidence records remain explicitly review-pending.

## Source review collections

Create labeled miniature collections and input metadata without rerunning OCR:

```bash
python review_assets.py \
	--archive KerdezzFelelek_ALAP.zip \
	--source-dir source \
	--output review/original \
	--pool-id original \
	--existing-json output/cards.json

python review_assets.py \
	--archive GyerekKérdezzFelelek.zip \
	--source-dir child_source \
	--output review/children \
	--pool-id children
```

The review output contains labeled source contact sheets, card contact sheets for the existing pool, and `input_source_metadata.json`. Existing card records are treated as cached; only explicitly flagged images or new source pools should be sent for further LLM evaluation.

For optional Azure OpenAI review, pass only a flagged candidate or contact sheet. Results are cached by image SHA-256, prompt, and deployment:

```bash
python llm_usage.py \
	--image review/original/flagged_candidate_contact_sheets/flagged_candidates_001.jpg \
	--cache review/llm_cache.json
```

## Processing master JSON

The target artifact is one hierarchical master JSON per input archive. The implementation plan and machine-readable stage contract are available here:

See [pipeline_implementation_plan.md](pipeline_implementation_plan.md) for the stage contracts, failure prevention, recovery behavior, and acceptance criteria. See [pipeline_spec.json](pipeline_spec.json) for the machine-readable contract.

The existing `pipeline.py` is a legacy SQLite-oriented ledger and must not be treated as the authoritative output. `master_pipeline.py` creates one JSON per pool with stage inputs, decisions, outputs, quality state, handoffs, source provenance, cards, orientation, and Hungarian quiz text. A stage will be marked `cached` only when its prior valid output has the same content-based cache key.

## Processing report

Generate the single presentation report for the whole pipeline with:

```bash
python generate_processing_report.py
```

The command writes [processing_report.md](processing_report.md). It reads the contract and any `pipeline/<pool>/processing_master.json` files, then reports contract validity, stage statuses, pool coverage, source/region/card/review counts, cache and failure counts, and the next automated gate. The report distinguishes validated planning from processing that has actually executed.