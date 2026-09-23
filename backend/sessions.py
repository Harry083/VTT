"""In-memory sessions: one per loaded folder. Holds the file list, the chosen
timestamp region and OCR settings, per-file results (including manual
corrections) and the state of the background processing run.

OCR is CPU-bound and blocking, so processing runs on a plain worker thread;
the API just reads the session's state when the frontend polls it.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from extract import extract_timestamp
from ocr_engine import TimestampReader

from .file_browser import list_videos

# Below this OCR confidence (on either end of a clip) a row is flagged for
# manual review even if both timestamps parsed - unless the two reads agree
# with the clip's length, which is stronger evidence than OCR confidence.
REVIEW_CONFIDENCE = 0.5

# If the first/last frame doesn't parse, retry this many whole seconds
# further in. The on-screen clock moves exactly one second per fps frames,
# so the offset can be subtracted back out exactly.
RETRY_SECONDS = (1, 2)


def duration_tolerance(duration: float) -> float:
    """How far end - start may drift from the clip's length (frame count /
    fps) and still count as agreeing - DVR clocks and variable frame rates
    are never exact."""
    return max(2.0, duration * 0.03)

_reader: Optional[TimestampReader] = None
_reader_lock = threading.Lock()


def get_reader() -> TimestampReader:
    """Load the OCR backend once per server process - model/binary startup is
    the expensive part, so it's shared across sessions and runs."""
    global _reader
    with _reader_lock:
        if _reader is None:
            _reader = TimestampReader()
        return _reader


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


@dataclass
class FileResult:
    path: Path
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None
    start_raw: Optional[str] = None
    end_raw: Optional[str] = None
    confidence: float = 0.0
    duration: Optional[float] = None  # clip length in seconds, from the container
    status: str = "pending"  # pending | ok | estimated | needs_review | error
    note: str = ""  # why a row needs review / was estimated
    estimated: Optional[str] = None  # "start" | "end" if derived from the clip length
    error: str = ""
    manual: bool = False  # True once the user has hand-corrected this row

    def public_dict(self, index: int) -> dict:
        return {
            "index": index,
            "name": self.path.name,
            "path": str(self.path),
            "start": _iso(self.start_dt),
            "end": _iso(self.end_dt),
            "start_raw": self.start_raw,
            "end_raw": self.end_raw,
            "confidence": self.confidence,
            "duration": self.duration,
            "status": self.status,
            "note": self.note,
            "estimated": self.estimated,
            "error": self.error,
            "manual": self.manual,
        }


class Clip:
    """An open video, for reading frames at specific positions."""

    def __init__(self, path: Path):
        self.path = path
        self.cap = cv2.VideoCapture(str(path))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 0.0
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        # Time from the first frame to the last - what end minus start on the
        # on-screen clock should come to. Some containers (.dav, raw .264)
        # report no/garbage fps or frame count, so it can be unknown.
        self.duration = (self.frame_count - 1) / self.fps if self.fps > 0 and self.frame_count > 1 else None

    def frame(self, index: int) -> Optional[np.ndarray]:
        if index < 0 or (self.frame_count and index >= self.frame_count):
            return None
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self) -> None:
        self.cap.release()


def read_frame(path: Path, which: str) -> Optional[np.ndarray]:
    """Read the first or last frame of a clip (BGR)."""
    cap = cv2.VideoCapture(str(path))
    try:
        if which == "last":
            last_idx = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) - 1
            if last_idx > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, last_idx)
        ok, frame = cap.read()
        if not ok and which == "last":
            # some containers (.dav, raw .264) report an unreliable frame
            # count - fall back to walking to the real last frame
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            frame = None
            while True:
                ok, f = cap.read()
                if not ok:
                    break
                frame = f
            return frame
        return frame if ok else None
    finally:
        cap.release()


def crop(frame: np.ndarray, region: tuple) -> np.ndarray:
    x, y, w, h = region
    fh, fw = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + w), min(fh, y + h)
    return frame[y0:y1, x0:x1]


@dataclass
class Session:
    id: str
    folder: Path
    files: list[FileResult]
    region: Optional[tuple] = None  # (x, y, w, h) in original frame pixels
    date_order: str = "DMY"
    hour_format: str = "24"
    status: str = "idle"  # idle | running | done | error | cancelled
    stage: str = ""
    processed: int = 0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "folder": str(self.folder),
            "region": list(self.region) if self.region else None,
            "date_order": self.date_order,
            "hour_format": self.hour_format,
            "status": self.status,
            "stage": self.stage,
            "processed": self.processed,
            "total": len(self.files),
            "error": self.error,
            "files": [f.public_dict(i) for i, f in enumerate(self.files)],
        }

    # ------------------------------------------------------------ processing
    def start_processing(self, region: tuple, date_order: str, hour_format: str) -> None:
        self.region = region
        self.date_order = date_order
        self.hour_format = hour_format
        self.status = "running"
        self.stage = "Loading OCR engine…"
        self.processed = 0
        self.error = None
        self.cancel_event.clear()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            reader = get_reader()
        except Exception as exc:  # noqa: BLE001 - OCR backend missing / failed to init
            self.status = "error"
            self.error = (
                f"Couldn't start the OCR engine: {exc}\n\n"
                "Check pytesseract is installed and the Tesseract binary is on PATH - see README."
            )
            return

        for i, result in enumerate(self.files):
            if self.cancel_event.is_set():
                self.status = "cancelled"
                self.stage = "Cancelled"
                return
            self.stage = f"Reading {result.path.name}"
            if not result.manual:  # don't clobber a value the user already corrected by hand
                self._process_one(result, reader)
            self.processed = i + 1

        self.status = "done"
        self.stage = "Done"

    def _read_end(self, clip: Clip, reader: TimestampReader, which: str):
        """Read the timestamp at the start or end of a clip, retrying a
        second or two further in if that frame doesn't parse. Returns
        (datetime or None, raw OCR text, confidence)."""
        last = clip.frame_count - 1
        first_try = None
        for offset in (0,) + (RETRY_SECONDS if clip.fps > 0 else ()):
            if which == "first":
                frame = clip.frame(int(round(offset * clip.fps))) if offset else read_frame(clip.path, "first")
            else:
                frame = clip.frame(last - int(round(offset * clip.fps))) if offset else read_frame(clip.path, "last")
            if frame is None:
                continue
            res = extract_timestamp(crop(frame, self.region), reader, self.date_order, self.hour_format)
            if first_try is None:
                first_try = res
            if res.datetime is not None:
                shift = timedelta(seconds=offset)
                dt = res.datetime - shift if which == "first" else res.datetime + shift
                return dt, res.raw_text, res.ocr_confidence
        if first_try is None:
            return None, None, 0.0
        return None, first_try.raw_text, first_try.ocr_confidence

    def _process_one(self, result: FileResult, reader: TimestampReader) -> None:
        result.estimated = None
        result.note = ""
        result.error = ""
        clip = Clip(result.path)
        try:
            result.duration = clip.duration
            start, start_raw, start_conf = self._read_end(clip, reader, "first")
            end, end_raw, end_conf = self._read_end(clip, reader, "last")
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't stop the batch
            result.status = "error"
            result.error = str(exc)
            return
        finally:
            clip.close()

        result.start_dt, result.start_raw = start, start_raw
        result.end_dt, result.end_raw = end, end_raw
        result.confidence = min(start_conf, end_conf)
        duration = result.duration

        if start and end:
            if duration is not None:
                drift = abs((end - start).total_seconds() - duration)
                if drift <= duration_tolerance(duration):
                    # two independent reads agreeing with the clip length
                    result.status = "ok"
                else:
                    result.status = "needs_review"
                    span = (end - start).total_seconds()
                    result.note = (
                        "The end reads earlier than the start" if span < 0
                        else f"Start and end are {timedelta(seconds=round(span))} apart, "
                             f"but the clip is {timedelta(seconds=round(duration))} long"
                    ) + " - one of them is misread"
            elif result.confidence >= REVIEW_CONFIDENCE:
                result.status = "ok"
            else:
                result.status = "needs_review"
                result.note = "Low OCR confidence"
        elif (start or end) and duration is not None and max(start_conf, end_conf) >= REVIEW_CONFIDENCE:
            # one end read cleanly - the other follows from the clip length
            if start:
                result.end_dt = start + timedelta(seconds=round(duration))
                result.estimated = "end"
            else:
                result.start_dt = end - timedelta(seconds=round(duration))
                result.estimated = "start"
            result.confidence = max(start_conf, end_conf)
            result.status = "estimated"
            result.note = f"The {result.estimated} timestamp couldn't be read, so it was worked out from the clip length"
        else:
            result.status = "needs_review"
            missing = [n for n, v in (("start", start), ("end", end)) if not v]
            result.note = f"Couldn't read the {' or '.join(missing)} timestamp"

    def test_ocr(self, index: int, region: tuple, date_order: str, hour_format: str) -> dict:
        """One-off extraction on a file's first frame, so the user can check
        the region and format pickers before running the whole batch."""
        frame = read_frame(self.files[index].path, "first")
        if frame is None:
            raise ValueError(f"Couldn't read a frame from {self.files[index].path.name}")
        res = extract_timestamp(crop(frame, region), get_reader(), date_order, hour_format)
        return {
            "raw_text": res.raw_text,
            "datetime": _iso(res.datetime),
            "confidence": res.ocr_confidence,
            "candidate": res.candidate_used,
        }

    def set_manual(self, index: int, start: datetime, end: datetime) -> None:
        r = self.files[index]
        r.start_dt, r.end_dt = start, end
        r.status = "ok"
        r.note = ""
        r.estimated = None
        r.manual = True


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self, folder: str) -> Session:
        files = list_videos(folder)
        session = Session(
            id=uuid.uuid4().hex[:12],
            folder=Path(folder),
            files=[FileResult(path=p) for p in files],
        )
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)


session_manager = SessionManager()
