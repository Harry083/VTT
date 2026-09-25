"""In-memory sessions: one per loaded folder. Holds the file list, the zones
and analysis settings, per-file results (events, activity, motion heatmap)
and the state of the background analysis run.

Decoding and detection are CPU/GPU-bound and blocking, so analysis runs on a
plain worker thread; the API just reads the session's state when the
frontend polls it.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from analyse import AnalysisSettings, analyse_video, snapshot
from detector import get_detector
from motion import render_heatmap

from .file_browser import list_videos


def read_first_frame(path: Path) -> Optional[np.ndarray]:
    cap = cv2.VideoCapture(str(path))
    try:
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


@dataclass
class FileResult:
    path: Path
    status: str = "pending"  # pending | running | done | partial | error
    progress: float = 0.0
    duration: Optional[float] = None
    width: int = 0
    height: int = 0
    events: list = field(default_factory=list)  # public event dicts
    activity: list = field(default_factory=list)  # per-second motion, 0..1
    heatmap: Optional[np.ndarray] = field(default=None, repr=False)
    detections_run: int = 0
    error: str = ""

    def public_dict(self, index: int) -> dict:
        return {
            "index": index,
            "name": self.path.name,
            "path": str(self.path),
            "status": self.status,
            "progress": self.progress,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "events": self.events,
            # rounded - this goes over the wire on every poll
            "activity": [round(a, 4) for a in self.activity],
            "detections_run": self.detections_run,
            "error": self.error,
        }


@dataclass
class Session:
    id: str
    folder: Path
    files: list[FileResult]
    settings: AnalysisSettings = field(default_factory=AnalysisSettings)
    model: str = "n"
    extra_model: str = ""
    detector_name: str = ""
    status: str = "idle"  # idle | running | done | error | cancelled
    stage: str = ""
    processed: int = 0
    run: int = 0  # bumped every analysis, so event ids (and snapshot URLs) never repeat
    error: Optional[str] = None
    warning: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    # event id -> (snapshot jpeg, thumbnail jpeg)
    snapshots: dict = field(default_factory=dict, repr=False)

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "folder": str(self.folder),
            "status": self.status,
            "stage": self.stage,
            "processed": self.processed,
            "total": len(self.files),
            "error": self.error,
            "warning": self.warning,
            "detector": self.detector_name,
            "files": [f.public_dict(i) for i, f in enumerate(self.files)],
        }

    # ------------------------------------------------------------ processing
    def start_processing(self, settings: AnalysisSettings, model: str, extra_model: str) -> None:
        self.settings = settings
        self.model = model
        self.extra_model = extra_model
        self.status = "running"
        self.stage = "Starting…"
        self.processed = 0
        self.run += 1
        self.error = None
        self.warning = None
        self.detector_name = ""
        self.cancel_event.clear()
        self.snapshots.clear()
        for f in self.files:
            f.status, f.progress, f.events, f.activity, f.heatmap, f.error = "pending", 0.0, [], [], None, ""
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        detector = None
        if self.settings.detect_objects and self.settings.categories:
            self.stage = "Loading object detection model…"
            try:
                detector = get_detector(self.model, self.extra_model)
            except Exception as exc:  # noqa: BLE001 - fall back, but say why
                if self.extra_model:
                    self.warning = f"Running without the extra model: {exc}"
                    try:
                        detector = get_detector(self.model)
                    except Exception as exc2:  # noqa: BLE001
                        self.warning = f"Object detection is off for this run: {exc2}"
                else:
                    self.warning = f"Object detection is off for this run: {exc}"
            if detector is not None:
                self.detector_name = detector.describe()

        for i, result in enumerate(self.files):
            if self.cancel_event.is_set():
                break
            self.stage = f"Analysing {result.path.name}"
            self._process_one(i, result, detector)
            self.processed = i + 1

        if self.cancel_event.is_set():
            self.status = "cancelled"
            self.stage = "Cancelled"
        else:
            self.status = "done"
            self.stage = "Done"

    def _process_one(self, index: int, result: FileResult, detector) -> None:
        result.status = "running"

        def progress(frac: float) -> None:
            result.progress = frac

        try:
            clip = analyse_video(result.path, self.settings, detector, progress, self.cancel_event.is_set)
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't stop the batch
            result.status = "error"
            result.error = str(exc)
            return

        events = []
        for n, ev in enumerate(clip.events):
            eid = f"{self.run}-{index}-{n}"
            self.snapshots[eid] = snapshot(ev)
            events.append({
                "id": eid,
                "file": index,
                "kind": ev.kind,
                "start": round(ev.start, 2),
                "end": round(ev.end, 2),
                "key_time": round(ev.key_time, 2),
                "peak": round(ev.peak, 4),
                "count": ev.count,
                "labels": sorted(ev.labels),
                "zones": sorted(ev.zones),
            })
            ev.key_frame = None  # the JPEGs are all we keep
        result.events = events
        result.duration = clip.duration
        result.width, result.height = clip.width, clip.height
        result.activity = clip.activity
        result.heatmap = clip.heatmap
        result.detections_run = clip.detections_run
        result.progress = 1.0
        # cancelled part-way through: keep what was found, but say it's incomplete
        result.status = "partial" if self.cancel_event.is_set() else "done"

    def heatmap_jpeg(self, index: int) -> Optional[bytes]:
        r = self.files[index]
        frame = read_first_frame(r.path)
        if frame is None or r.heatmap is None:
            return None
        ok, buf = cv2.imencode(".jpg", render_heatmap(frame, r.heatmap), [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes() if ok else None


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
