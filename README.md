# VTT — CCTV Timestamp OCR & Timeline

A local web app that reads the burned-in timestamp off the first and last
frame of every clip in a folder of CCTV exports, then lays the clips out on a
timeline so you can see coverage and gaps at a glance.

## Setup

Requires Python 3.10+ and the Tesseract OCR binary (see
[Tesseract setup](#tesseract-setup-windows) below).

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```bash
.venv\Scripts\python.exe run.py
```

Then open http://localhost:8757

1. **Folder** — paste a path or use *Browse…* to pick the folder of exports.
2. **Timestamp region** — drag a box around the on-screen timestamp on a
   preview frame. *Test OCR* reads that frame straight away so you can check
   the box and the date/time format before running the whole batch.
3. **Date order / time format** — set these to match what's on screen, then
   *Process Files*. Rows fill in as each file is read. Every timestamp in the
   app is shown, and typed, in this format. 12-hour overlays without AM/PM
   are fine.
4. **Review** — each file's start and end are checked against the clip's
   own length (from the video container): if they agree, the row is OK; if
   they don't, it's flagged for review. If only one end can be read, the
   other is worked out from the clip length and marked *Estimated*. The
   timeline shows every clip (green = OK, dim green = estimated, amber =
   needs review, blue = manually corrected), with the gaps between clips
   shaded and summarised above it. Hover a bar for details, click it to open
   the clip in your default player. *Edit* on a file row shows the cropped
   first/last frames next to what OCR read, pre-filled so a misread is
   usually a one-character fix, with buttons to fill one end in from the
   other plus the clip length. Reprocessing leaves hand-corrected rows alone.
5. **Export CSV** — one row per file, sorted by start time.

## Pipeline

The web app (`backend/` + `frontend/`) is a thin layer over the OCR
pipeline modules, which can also be used on their own:

1. **`preprocessing.py`** — cheap, colour-aware binarisation. Generates
   white (at several brightness cut-offs)/black/green/adaptive candidate
   masks, blanks anything outside the text line, and orders them by
   connected-component geometry (no OCR needed for scoring). Handles mixed
   footage (different overlay colours across a batch) and white text with no
   backing plate over busy backgrounds without needing to know the style in
   advance.
2. **`ocr_engine.py`** — runs Tesseract (via pytesseract) on a candidate.
3. **`timestamp_parser.py`** — turns the raw OCR string into a `datetime` by
   extracting exactly the digit groups expected for a user-chosen date order
   (DD-MM-YYYY / MM-DD-YYYY / YYYY-MM-DD) and hour format (24h / 12h, AM/PM
   optional), correcting common OCR digit confusions (O/0, l/1/I, S/5, B/8),
   tolerating a `/` misread as `1`/`7`, and ignoring stray non-digit noise
   (camera labels, "REC", etc.) rather than letting it leak into the result.
4. **`extract.py`** — wires the three together:
   `extract_timestamp(crop, reader, date_order, hour_format)`. OCRs the
   candidates best-first and prefers a read that parses in the expected
   format over raw OCR confidence, stopping early once two candidates agree.
5. **`backend/`** — FastAPI app (`main.py`) plus per-folder sessions and
   the background processing thread (`sessions.py`) and the server-side
   folder browser (`file_browser.py`). **`frontend/`** is the vanilla
   HTML/CSS/JS UI.

## Why this design

- **Preprocessing does the format-agnosticism, not the OCR engine.** Rather
  than one generic grayscale threshold, we generate the handful of
  binarisations that actually correspond to real overlay styles and pick
  the best by shape, not colour assumptions baked into the scorer.
- **Cost stays low.** All preprocessing is threshold/morphology on a small
  crop — no per-frame OCR, no heavy filtering. On synthetic samples the
  white/black/green candidates each score >0.9 for their matching overlay
  style once upscaled.
- **Parse success beats OCR confidence.** On a busy background the most
  "confident" read is often confidently wrong, so candidates are judged by
  whether their text parses in the expected format. On clean overlays the
  first two candidates agree and the rest are never OCR'd.
- **Per-file, not per-frame.** Call `extract_timestamp` once on the first
  frame and once on the last frame of each clip (per your original
  workflow) — `TimestampReader` should be instantiated once per session and
  reused, since model load time is the expensive part.
- **Strict format extraction, not fuzzy guessing.** `timestamp_parser.py`
  no longer tries to auto-detect the date/time shape (that's how stray OCR
  noise - a camera label, "REC", a misread separator - was ending up in
  results). It's told the date order and hour format up front (via the UI
  pickers) and extracts exactly those digit groups, discarding everything
  else as noise. This also removes the DD/MM vs MM/DD ambiguity a generic
  parser has to guess at.

## Tesseract setup (Windows)

Tesseract isn't pure-Python - `pip install pytesseract` only installs the
wrapper, not the OCR engine itself:

1. Install the binary: the
   [UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki) is the
   standard Windows installer.
2. Either add it to PATH, or point pytesseract at it explicitly:
   ```python
   reader = TimestampReader(tesseract_cmd="C:/Program Files/Tesseract-OCR/tesseract.exe")
   ```

Note: `tessedit_char_whitelist` (restricting Tesseract to digits/separators)
was deliberately left out — on the tested Tesseract build it made
`image_to_data` report 0% confidence on every word even when the text was
read correctly, which broke the "keep the highest-confidence candidate"
logic. The preprocessing step already isolates the timestamp cleanly enough
that stray characters are rare, and `timestamp_parser.py` strips whatever
noise does get through.

## Wiring into the file loop

```python
from ocr_engine import TimestampReader
from extract import extract_timestamp
import cv2

reader = TimestampReader()  # create once per session

def get_start_end(video_path, region, date_order="DMY", hour_format="24"):
    cap = cv2.VideoCapture(video_path)
    x, y, w, h = region

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ok, frame = cap.read()
    start = extract_timestamp(frame[y:y+h, x:x+w], reader, date_order, hour_format) if ok else None

    last_frame_idx = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) - 1
    cap.set(cv2.CAP_PROP_POS_FRAMES, last_frame_idx)
    ok, frame = cap.read()
    end = extract_timestamp(frame[y:y+h, x:x+w], reader, date_order, hour_format) if ok else None

    cap.release()
    return start, end
```

## Flagging for manual review

`TimestampExtractionResult` carries `ocr_confidence` and `datetime` (`None`
if parsing failed). Use both as your manual-override trigger:

```python
result = extract_timestamp(crop, reader)
if result.datetime is None or result.ocr_confidence < 0.5:
    # surface in the UI for the user to correct/enter manually
    ...
```

## Project structure

```
VTT/
├── backend/
│   ├── main.py            FastAPI app & routes
│   ├── sessions.py        per-folder state, background OCR processing
│   └── file_browser.py    server-side directory listing for the folder picker
├── frontend/              vanilla HTML/CSS/JS UI
├── preprocessing.py       candidate binarisation for the timestamp crop
├── ocr_engine.py          Tesseract wrapper
├── timestamp_parser.py    strict date/time extraction from OCR text
├── extract.py             preprocess → OCR → parse for one crop
├── run.py                 web app entry point (uvicorn)
└── requirements.txt
```

## License

MIT — see [LICENSE](LICENSE).
