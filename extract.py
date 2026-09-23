"""
extract.py

Ties the pipeline together: preprocess a frame crop -> OCR the best
candidate(s) -> parse into a datetime. This is the function the rest of the
app (file loop, timeline builder) calls per frame.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import numpy as np

from preprocessing import preprocess_timestamp_region
from ocr_engine import TimestampReader
from timestamp_parser import parse_timestamp


@dataclass
class TimestampExtractionResult:
    datetime: Optional[datetime]
    raw_text: Optional[str]
    ocr_confidence: float
    candidate_used: Optional[str]


def extract_timestamp(
    crop_bgr: np.ndarray,
    reader: TimestampReader,
    date_order: str = "DMY",
    hour_format: str = "24",
    upscale_factor: float = 3.0,
) -> TimestampExtractionResult:
    """
    crop_bgr: the on-screen timestamp region, cropped from a video frame
    (BGR, as returned by cv2.VideoCapture).
    reader: a TimestampReader instance - create once per session/file batch,
    not per frame, since loading the OCR model has real startup cost.
    date_order: "DMY", "MDY" or "YMD" - matches the on-screen format.
    hour_format: "24" or "12".
    """
    candidates = preprocess_timestamp_region(crop_bgr, upscale_factor=upscale_factor)

    # OCR candidates best-scored first, and prefer reads that actually parse
    # in the expected format over raw OCR confidence - on a busy background
    # the most "confident" read is often confidently wrong. Two candidates
    # agreeing on the same timestamp is strong evidence, so stop there.
    best_parsed = None  # (OcrResult, datetime)
    best_raw = None
    seen = {}
    for candidate in candidates:
        ocr_result = reader.read(candidate)
        if ocr_result is None:
            continue
        if best_raw is None or ocr_result.confidence > best_raw.confidence:
            best_raw = ocr_result
        dt = parse_timestamp(ocr_result.text, date_order=date_order, hour_format=hour_format)
        if dt is None:
            continue
        if best_parsed is None or ocr_result.confidence > best_parsed[0].confidence:
            best_parsed = (ocr_result, dt)
        if dt in seen:
            agreed = max(seen[dt], ocr_result, key=lambda r: r.confidence)
            best_parsed = (agreed, dt)
            break
        seen[dt] = ocr_result

    if best_parsed is not None:
        ocr_result, dt = best_parsed
    elif best_raw is not None:
        ocr_result, dt = best_raw, None
    else:
        return TimestampExtractionResult(None, None, 0.0, None)

    return TimestampExtractionResult(
        datetime=dt,
        raw_text=ocr_result.text,
        ocr_confidence=ocr_result.confidence,
        candidate_used=ocr_result.candidate_name,
    )
