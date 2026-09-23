"""
ocr_engine.py

Thin wrapper around Tesseract (via pytesseract) so the rest of the pipeline
just asks "read this preprocessed candidate" and gets back text + confidence.
extract.py runs it over the preprocessing candidates and decides which read
to trust.

Tesseract needs no heavy ML dependency (just the binary + pytesseract), and
combined with the preprocessing module's candidate binarisation it reads
timestamps far more reliably than feeding it a raw, un-processed crop - the
poor accuracy originally seen was mostly a preprocessing problem, not a
Tesseract problem.

Windows setup: install the binary itself (e.g. the UB-Mannheim build:
https://github.com/UB-Mannheim/tesseract/wiki) and either add it to PATH or
pass its path explicitly:
    TimestampReader(tesseract_cmd="C:/Program Files/Tesseract-OCR/tesseract.exe")
"""

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import pytesseract


@dataclass
class OcrResult:
    text: str
    confidence: float  # 0.0-1.0
    candidate_name: str  # which preprocessing candidate produced this (e.g. "white205")


class TimestampReader:
    def __init__(self, tesseract_cmd: Optional[str] = None):
        """
        tesseract_cmd: optional explicit path to tesseract.exe on Windows,
        if it's not on PATH - see module docstring.
        """
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    def _read(self, image: np.ndarray) -> Optional[tuple]:
        # --psm 7: treat the crop as a single line of text (matches a
        # timestamp overlay much better than tesseract's default page-layout
        # assumption, which is a large part of why raw accuracy was poor).
        # Deliberately NOT using tessedit_char_whitelist here: on some
        # tesseract builds it makes image_to_data report every word at 0%
        # confidence even when the text itself is read correctly. The
        # character set is narrow enough (digits/separators) that stray
        # characters are rare, and timestamp_parser.py already strips noise
        # and corrects common OCR digit confusions during parsing.
        config = "--psm 7"
        # Candidates are white text on black; Tesseract's LSTM model is
        # trained on dark text on a light page, and wants a margin around
        # the text - both noticeably improve reads on busy backgrounds.
        if image.ndim == 2:
            image = cv2.copyMakeBorder(255 - image, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
        data = pytesseract.image_to_data(image, config=config, output_type=pytesseract.Output.DICT)
        # conf is an int, float or numeric string depending on pytesseract version
        words = [(t, float(c)) for t, c in zip(data["text"], data["conf"]) if t.strip() and float(c) >= 0]
        if not words:
            return None
        text = " ".join(t for t, _ in words).strip()
        # tesseract confidences are 0-100 per word; normalise to 0-1
        confidence = sum(c for _, c in words) / len(words) / 100.0
        return text, confidence

    def read(self, candidate) -> Optional[OcrResult]:
        """OCR a single preprocessing.Candidate."""
        result = self._read(candidate.image)
        if result is None:
            return None
        text, confidence = result
        return OcrResult(text=text, confidence=confidence, candidate_name=candidate.name)
