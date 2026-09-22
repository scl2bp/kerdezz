# Kerdezz card extraction

This project extracts the 3x3 Hungarian card sheets from `KerdezzFelelek_ALAP.zip` into standalone JPEG images and OCR data. The supplied archive contains 31 sheets and 279 cards.

## Prerequisites

Install Tesseract and its Hungarian language data:

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-hun
python -m pip install -r requirements.txt
```

## Run

```bash
python extract_cards.py KerdezzFelelek_ALAP.zip --output output
```

The command creates:

- `output/cards/card_001.jpg` through `card_279.jpg`
- `output/cards.json`, containing the source location, image path, OCR category, clues, answer, and raw OCR text for every card

The raw OCR is kept because the source scans contain occasional hyphenated line breaks and characters that may need manual review.

## Source review collections

Create labeled miniature collections and input metadata without rerunning OCR:

```bash
python review_assets.py \
	--archive KerdezzFelelek_ALAP.zip \
	--source-dir kerdezz/source \
	--output kerdezz/output \
	--pool-id kerdezz \
	--existing-json kerdezz/cards.json

python review_assets.py \
	--archive GyerekKérdezzFelelek.zip \
	--source-dir gyerek/source \
	--output gyerek/output \
	--pool-id gyerek \
	--existing-json gyerek/cards.json
```

The review output contains labeled source contact sheets, card contact sheets for the existing pool, and `input_source_metadata.json`. Existing card records are treated as cached; only explicitly flagged images or new source pools should be sent for further LLM evaluation.

For optional Azure OpenAI review, pass only a flagged candidate or contact sheet. Results are cached by image SHA-256, prompt, and deployment:

```bash
python llm_usage.py \
	--image review/original/flagged_candidate_contact_sheets/flagged_candidates_001.jpg \
	--cache review/llm_cache.json
```

## Pipeline ledger and artifact database

Build the complete top-to-bottom lineage without repeating sufficient work:

```bash
python pipeline.py
```

This writes one JSON document per stage under `pipeline/<pool>/stages/` and updates `output/artifact_database.sqlite3`. The database stores card category, answer, every clue, combined quiz text, raw OCR, image provenance, stage fingerprints, and cached LLM review records. A stage is marked `cached` when its input payload and parameters have not changed.