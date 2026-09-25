"""
analyse.py

Ties the pipeline together for one clip: sample frames -> motion detection
on every sample -> object detection on the samples worth looking at ->
events. This is the function the web app's worker calls per file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from detector import ObjectDetector
from events import Event, EventBuilder
from motion import MotionDetector, Zone

# Motion samples further apart than this split into separate events.
MOTION_GAP = 2.0
# A single moving sample is usually noise (compression glitch, insect, rain).
MOTION_MIN_SAMPLES = 2
# While motion was seen within this many seconds, the object detector runs.
MOTION_HOLD = 2.0
# Assumed when a container (.dav, raw .264) reports no frame rate.
FALLBACK_FPS = 25.0

# BGR, matching the category colours in the frontend
COLOURS = {
    "motion": (255, 208, 127),
    "person": (142, 207, 62),
    "vehicle": (255, 140, 177),
    "weapon": (90, 90, 240),
    "bag": (66, 185, 245),
    "animal": (178, 164, 154),
    "scene_change": (178, 164, 154),
}

SNAPSHOT_WIDTH = 960
THUMB_WIDTH = 200


@dataclass
class AnalysisSettings:
    sensitivity: str = "medium"  # low | medium | high
    sample_fps: float = 5.0  # frames per second fed to motion detection
    zones: list = field(default_factory=list)  # [Zone, ...]
    detect_objects: bool = True
    categories: set = field(default_factory=lambda: {"person", "vehicle", "weapon", "bag"})
    confidence: float = 0.4
    detect_every: float = 1.0  # seconds between object detection passes
    motion_gated: bool = True  # only run the detector around motion


@dataclass
class ClipAnalysis:
    duration: Optional[float]
    fps: float
    width: int
    height: int
    events: list  # [Event, ...] sorted by start
    activity: list  # per second of clip: largest fraction of the frame moving
    heatmap: Optional[np.ndarray]
    samples: int
    detections_run: int


def _draw(frame: np.ndarray, boxes: list, colour: tuple) -> np.ndarray:
    img = frame.copy()
    thick = max(2, frame.shape[1] // 400)
    scale = max(0.5, frame.shape[1] / 1600)
    c = colour
    for (x, y, w, h), caption in boxes:
        cv2.rectangle(img, (x, y), (x + w, y + h), c, thick)
        if caption:
            (tw, th), base = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
            ty = max(y, th + base + 4)
            cv2.rectangle(img, (x, ty - th - base - 4), (x + tw + 6, ty), c, -1)
            cv2.putText(img, caption, (x + 3, ty - base - 2), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 1, cv2.LINE_AA)
    return img


def _jpeg(img: np.ndarray, width: int, quality: int = 85) -> bytes:
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(round(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


def snapshot(ev: Event) -> tuple[bytes, bytes]:
    """(full-size, thumbnail) JPEGs of an event's key frame with its boxes drawn."""
    if ev.key_frame is None:
        return b"", b""
    img = _draw(ev.key_frame, ev.key_boxes, COLOURS.get(ev.kind, (255, 255, 255)))
    return _jpeg(img, SNAPSHOT_WIDTH), _jpeg(img, THUMB_WIDTH, 75)


def analyse_video(
    path: Path,
    settings: AnalysisSettings,
    detector: Optional[ObjectDetector] = None,
    progress: Callable[[float], None] = lambda f: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> ClipAnalysis:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Couldn't open {path.name}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if not 0 < fps < 240:
            fps = FALLBACK_FPS
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        step = max(1, int(round(fps / settings.sample_fps)))

        motion_events = EventBuilder("motion", MOTION_GAP, MOTION_MIN_SAMPLES)
        scene_events = EventBuilder("scene_change", MOTION_GAP)
        object_gap = max(3.0, settings.detect_every * 2.5)
        object_events = {c: EventBuilder(c, object_gap) for c in settings.categories}

        motion: Optional[MotionDetector] = None
        activity: list[float] = []
        last_motion = -1e9
        last_detect = -1e9
        samples = detections_run = 0
        width = height = 0
        idx = -1

        while True:
            if not cap.grab():
                break
            idx += 1
            if idx % step:
                continue
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue
            if cancelled():
                break
            t = idx / fps
            samples += 1
            if motion is None:
                height, width = frame.shape[:2]
                motion = MotionDetector(frame.shape, settings.zones, settings.sensitivity)

            m = motion.update(frame)
            second = int(t)
            while len(activity) <= second:
                activity.append(0.0)
            if m.scene_change:
                scene_events.observe(t, True, m.area, frame)
            else:
                activity[second] = max(activity[second], m.area)
                if m.boxes:
                    last_motion = t
                motion_events.observe(
                    t, bool(m.boxes), m.area, frame,
                    [(b, "") for b in m.boxes], len(m.boxes), zones=m.zones,
                )

            want_objects = (
                detector is not None
                and settings.categories
                and t - last_detect >= settings.detect_every - 1e-6
                and (not settings.motion_gated or t - last_motion <= MOTION_HOLD)
            )
            if want_objects:
                last_detect = t
                detections_run += 1
                found = [
                    d for d in detector.detect(frame, settings.confidence, settings.categories)
                    if in_zones(d.box, settings.zones)
                ]
                for cat, builder in object_events.items():
                    dets = [d for d in found if d.category == cat]
                    builder.observe(
                        t, bool(dets),
                        max((d.confidence for d in dets), default=0.0),
                        frame,
                        [(d.box, f"{d.label} {d.confidence:.0%}") for d in dets],
                        len(dets),
                        labels={d.label for d in dets},
                    )

            if frame_count > 0 and samples % 10 == 0:
                progress(min(idx / frame_count, 1.0))

        duration = (idx / fps) if idx > 0 else None
        events = motion_events.finish() + scene_events.finish()
        for builder in object_events.values():
            events += builder.finish()
        events.sort(key=lambda e: (e.start, e.kind))
        return ClipAnalysis(
            duration=duration,
            fps=fps,
            width=width,
            height=height,
            events=events,
            activity=activity,
            heatmap=motion.heatmap if motion is not None else None,
            samples=samples,
            detections_run=detections_run,
        )
    finally:
        cap.release()


def in_zones(box: tuple, zones: list[Zone]) -> bool:
    """Zones apply to objects too: one centred in an ignore zone is dropped,
    and with watch zones set it has to overlap one of them."""
    x, y, w, h = box
    cx, cy = x + w / 2, y + h / 2
    for z in zones:
        if z.mode == "ignore" and z.x <= cx < z.x + z.w and z.y <= cy < z.y + z.h:
            return False
    watch = [z for z in zones if z.mode == "watch"]
    return not watch or any(x < z.x + z.w and z.x < x + w and y < z.y + z.h and z.y < y + h for z in watch)


def zones_from_dicts(items: list[dict]) -> list[Zone]:
    zones = []
    watch_n = ignore_n = 0
    for z in items:
        mode = z.get("mode", "watch")
        if mode == "watch":
            watch_n += 1
            name = z.get("name") or f"Zone {watch_n}"
        else:
            ignore_n += 1
            name = z.get("name") or f"Ignore {ignore_n}"
        zones.append(Zone(int(z["x"]), int(z["y"]), int(z["w"]), int(z["h"]), mode, name))
    return zones
