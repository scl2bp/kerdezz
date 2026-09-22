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