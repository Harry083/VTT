"""
motion.py

Finds where in the frame things are moving. Frames are downscaled and fed to
an OpenCV MOG2 background model; the foreground mask is cleaned up, clipped
to the user's watch/ignore zones and turned into bounding boxes.

Deliberately cheap - this runs on every sampled frame, and its output decides
which frames the (much more expensive) object detector looks at.
"""

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

# Frames are analysed at this width - plenty for motion, and keeps MOG2 fast
# regardless of whether the footage is D1 or 4K.
ANALYSIS_WIDTH = 480

# Sensitivity presets: MOG2 variance threshold (lower = picks up subtler
# changes) and the smallest blob that counts, as a fraction of the frame.
SENSITIVITY = {
    "low": {"var_threshold": 40, "min_area": 0.004},
    "medium": {"var_threshold": 25, "min_area": 0.0012},
    "high": {"var_threshold": 16, "min_area": 0.0004},
}

# More than this fraction of the frame changing at once is a lighting change
# (IR cut-over, lights on/off, auto-exposure) or the camera moving - not
# something moving through the scene.
SCENE_CHANGE_FRACTION = 0.55

# Samples ignored while the background model learns the scene.
WARMUP_SAMPLES = 5


@dataclass
class Zone:
    """A rectangle in original frame pixels. `mode` is "watch" (only motion
    inside watch zones counts) or "ignore" (motion here never counts - e.g.
    the burned-in clock, a flag, trees)."""
    x: int
    y: int
    w: int
    h: int
    mode: str = "watch"
    name: str = ""


@dataclass
class MotionResult:
    boxes: list  # [(x, y, w, h), ...] in original frame pixels
    area: float  # fraction of the (zoned) frame that moved, 0..1
    zones: list  # names of watch zones the motion touched
    scene_change: bool = False
    mask: Optional[np.ndarray] = field(default=None, repr=False)  # analysis-res foreground


class MotionDetector:
    """Feed frames in order with `update`; one instance per clip."""

    def __init__(self, frame_shape: tuple, zones: list[Zone], sensitivity: str = "medium"):
        preset = SENSITIVITY[sensitivity]
        fh, fw = frame_shape[:2]
        self.scale = min(1.0, ANALYSIS_WIDTH / fw)
        self.size = (max(1, int(round(fw * self.scale))), max(1, int(round(fh * self.scale))))
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=300, varThreshold=preset["var_threshold"], detectShadows=True
        )
        self.samples = 0
        self.zones = zones
        self.watch = [z for z in zones if z.mode == "watch"]

        aw, ah = self.size
        # Which analysis pixels are allowed to count as motion
        if self.watch:
            self.allowed = np.zeros((ah, aw), np.uint8)
            for z in self.watch:
                self._fill(self.allowed, z, 255)
        else:
            self.allowed = np.full((ah, aw), 255, np.uint8)
        for z in zones:
            if z.mode == "ignore":
                self._fill(self.allowed, z, 0)
        allowed_px = max(int(cv2.countNonZero(self.allowed)), 1)
        self.allowed_px = allowed_px
        self.min_area_px = max(4, preset["min_area"] * allowed_px)
        self.heatmap = np.zeros((ah, aw), np.float32)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    def _fill(self, mask: np.ndarray, z: Zone, value: int) -> None:
        s = self.scale
        x0, y0 = int(z.x * s), int(z.y * s)
        x1, y1 = int(np.ceil((z.x + z.w) * s)), int(np.ceil((z.y + z.h) * s))
        mask[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = value

    def update(self, frame: np.ndarray) -> MotionResult:
        small = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        # colour, not grayscale - a red coat on grey paving is nearly the same brightness
        fg = self.bg.apply(cv2.GaussianBlur(small, (5, 5), 0))
        self.samples += 1
        if self.samples <= WARMUP_SAMPLES:
            return MotionResult([], 0.0, [])

        # MOG2 marks shadows as 127 - drop them, they follow people around
        _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
        fg = cv2.bitwise_and(fg, self.allowed)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.kernel)
        fg = cv2.dilate(fg, self.kernel, iterations=2)

        changed = cv2.countNonZero(fg) / self.allowed_px
        if changed > SCENE_CHANGE_FRACTION:
            return MotionResult([], changed, [], scene_change=True)

        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        area = 0.0
        keep = np.zeros_like(fg)
        for c in contours:
            a = cv2.contourArea(c)
            if a < self.min_area_px:
                continue
            area += a
            cv2.drawContours(keep, [c], -1, 255, -1)
            x, y, w, h = cv2.boundingRect(c)
            boxes.append(tuple(int(round(v / self.scale)) for v in (x, y, w, h)))
        if not boxes:
            return MotionResult([], 0.0, [])

        self.heatmap += keep.astype(np.float32) / 255.0
        return MotionResult(
            boxes=boxes,
            area=area / self.allowed_px,
            zones=[z.name for z in self.watch if any(_overlaps(b, z) for b in boxes)],
            mask=keep,
        )


def _overlaps(box: tuple, z: Zone) -> bool:
    x, y, w, h = box
    return x < z.x + z.w and z.x < x + w and y < z.y + z.h and z.y < y + h


def render_heatmap(frame: np.ndarray, heatmap: np.ndarray) -> np.ndarray:
    """Overlay accumulated motion on a frame - where in the scene things moved
    over the whole clip."""
    fh, fw = frame.shape[:2]
    if heatmap.max() <= 0:
        return frame
    hm = cv2.resize(heatmap, (fw, fh), interpolation=cv2.INTER_LINEAR)
    # log scale so a few passes still show next to a constantly busy area
    hm = np.log1p(hm) / np.log1p(hm.max())
    colour = cv2.applyColorMap((hm * 255).astype(np.uint8), cv2.COLORMAP_JET)
    alpha = np.clip(hm * 1.5, 0, 0.65)[..., None]
    dimmed = (frame * 0.7).astype(np.float32)
    return (dimmed * (1 - alpha) + colour.astype(np.float32) * alpha).astype(np.uint8)
