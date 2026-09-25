"""
events.py

Turns per-sample observations ("motion at t=12.4s", "2 people at t=13.0s")
into events with a start and end - what the user actually reviews. Samples
of the same kind closer together than `gap` seconds join one event, so a
person walking behind a pillar stays one sighting rather than three.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class Event:
    kind: str  # "motion" | "scene_change" | a detector category ("person", "vehicle", ...)
    start: float  # seconds into the clip
    end: float
    peak: float = 0.0  # motion: largest fraction of the frame moving; objects: best confidence
    count: int = 0  # objects: most seen in one frame
    samples: int = 0
    labels: set = field(default_factory=set)  # model class names seen ("car", "truck")
    zones: set = field(default_factory=set)  # watch zones the motion touched
    key_time: float = 0.0  # the sample shown as this event's snapshot
    key_frame: Optional[np.ndarray] = field(default=None, repr=False)
    key_boxes: list = field(default_factory=list, repr=False)  # [(box, caption), ...]


class EventBuilder:
    """One per event kind. Call `observe` for every sample where this kind
    was looked for, then `finish` at the end of the clip."""

    def __init__(self, kind: str, gap: float, min_samples: int = 1):
        self.kind = kind
        self.gap = gap
        self.min_samples = min_samples
        self.current: Optional[Event] = None
        self.done: list[Event] = []

    def observe(
        self,
        t: float,
        active: bool,
        score: float = 0.0,
        frame: Optional[np.ndarray] = None,
        boxes: Optional[list] = None,
        count: int = 0,
        labels=(),
        zones=(),
    ) -> None:
        ev = self.current
        if ev is not None and t - ev.end > self.gap:
            self._close()
            ev = None
        if not active:
            return
        if ev is None:
            ev = self.current = Event(self.kind, start=t, end=t)
        ev.end = t
        ev.samples += 1
        ev.count = max(ev.count, count)
        ev.labels.update(labels)
        ev.zones.update(zones)
        # keep the strongest sample as the snapshot (copied - callers reuse frames)
        if score > ev.peak or ev.key_frame is None:
            ev.peak = max(ev.peak, score)
            ev.key_time = t
            ev.key_frame = frame.copy() if frame is not None else None
            ev.key_boxes = list(boxes or [])

    def _close(self) -> None:
        if self.current is not None and self.current.samples >= self.min_samples:
            self.done.append(self.current)
        self.current = None

    def finish(self) -> list[Event]:
        self._close()
        return self.done
