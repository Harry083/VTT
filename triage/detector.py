"""
detector.py

Object detection with an Ultralytics YOLO model. The model's own class names
(COCO's 80 for the stock weights, whatever a custom model was trained on) are
folded into the handful of categories an investigator actually triages by:
person, vehicle, weapon, bag, animal.

The stock COCO weights only know "knife" as a weapon. To pick up firearms,
point `extra_model` at a model trained for them (any YOLO .pt/.onnx whose
class names include gun / pistol / rifle / ...) - its detections are merged
with the main model's.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

MODELS_DIR = Path(__file__).resolve().parent / "models"

# Stock model sizes offered in the UI - n is fast enough on CPU, s/m trade
# speed for catching smaller / more distant objects.
STOCK_MODELS = {"n": "yolo11n.pt", "s": "yolo11s.pt", "m": "yolo11m.pt"}

CATEGORIES = ("person", "vehicle", "weapon", "bag", "animal")

# Class name -> category. Checked as exact names first, then as keywords, so
# custom models ("handgun", "Pistol", "assault_rifle") fold in too.
CLASS_CATEGORIES = {
    "person": "person", "pedestrian": "person", "people": "person",
    "car": "vehicle", "truck": "vehicle", "bus": "vehicle", "motorcycle": "vehicle",
    "motorbike": "vehicle", "bicycle": "vehicle", "van": "vehicle", "train": "vehicle",
    "boat": "vehicle", "scooter": "vehicle",
    "knife": "weapon", "baseball bat": "weapon",
    "backpack": "bag", "handbag": "bag", "suitcase": "bag",
    "dog": "animal", "cat": "animal", "horse": "animal", "bird": "animal",
    "sheep": "animal", "cow": "animal", "bear": "animal",
}
# Substrings, so "handgun" / "shotgun" / "assault rifle" match
WEAPON_KEYWORDS = ("gun", "pistol", "rifle", "firearm", "weapon", "revolver", "knife", "machete", "sword")
# Whole words, so "car" doesn't catch "carrot"
WEAPON_WORDS = {"blade", "axe", "bat", "crowbar", "hammer"}
PERSON_WORDS = {"person", "human", "man", "woman", "child", "face"}
VEHICLE_WORDS = {"car", "vehicle", "truck", "van", "bike", "motorbike", "bus", "lorry", "taxi", "suv"}


def category_for(class_name: str) -> Optional[str]:
    name = class_name.strip().lower().replace("_", " ").replace("-", " ")
    if name in CLASS_CATEGORIES:
        return CLASS_CATEGORIES[name]
    words = set(name.split())
    if any(k in name for k in WEAPON_KEYWORDS) or words & WEAPON_WORDS:
        return "weapon"
    if words & PERSON_WORDS:
        return "person"
    if words & VEHICLE_WORDS:
        return "vehicle"
    return None


@dataclass
class Detection:
    category: str
    label: str  # the model's own class name
    confidence: float
    box: tuple  # (x, y, w, h) in original frame pixels


class ObjectDetector:
    """Wraps one or two YOLO models. Loading is the slow part, so instances
    are cached per process with `get_detector`."""

    def __init__(self, model: str = "n", extra_model: str = ""):
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "Object detection needs the ultralytics package: pip install ultralytics"
            ) from exc

        self.models = [YOLO(resolve_model(model))]
        if extra_model:
            path = Path(extra_model).expanduser()
            if not path.is_file():
                raise RuntimeError(f"Extra model not found: {extra_model}")
            self.models.append(YOLO(str(path)))
        self.names = [m.names for m in self.models]
        self.lock = threading.Lock()  # models aren't safe to call from two threads at once

    def describe(self) -> str:
        return " + ".join(Path(str(m.ckpt_path or m.model_name)).name for m in self.models)

    def detect(self, frame: np.ndarray, confidence: float, categories: set[str]) -> list[Detection]:
        out = []
        with self.lock:
            for model, names in zip(self.models, self.names):
                result = model.predict(frame, conf=confidence, verbose=False)[0]
                if result.boxes is None:
                    continue
                for xyxy, conf, cls in zip(
                    result.boxes.xyxy.cpu().numpy(),
                    result.boxes.conf.cpu().numpy(),
                    result.boxes.cls.cpu().numpy().astype(int),
                ):
                    label = names.get(int(cls), str(cls)) if isinstance(names, dict) else names[int(cls)]
                    category = category_for(label)
                    if category is None or category not in categories:
                        continue
                    x0, y0, x1, y1 = (int(round(v)) for v in xyxy)
                    out.append(Detection(category, label, float(conf), (x0, y0, x1 - x0, y1 - y0)))
        return out


def resolve_model(model: str) -> str:
    """A stock size ("n"/"s"/"m") -> weights file in triage/models/.
    Ultralytics downloads it there on first use; on an offline machine,
    copy the .pt file in by hand."""
    MODELS_DIR.mkdir(exist_ok=True)
    return str(MODELS_DIR / STOCK_MODELS.get(model, model))


_detectors: dict = {}
_detectors_lock = threading.Lock()


def get_detector(model: str = "n", extra_model: str = "") -> ObjectDetector:
    key = (model, extra_model)
    with _detectors_lock:
        if key not in _detectors:
            _detectors[key] = ObjectDetector(model, extra_model)
        return _detectors[key]


def backend_status() -> dict:
    """What the UI shows next to the object settings - whether detection can
    run at all, and on what hardware."""
    try:
        import ultralytics  # noqa: F401
        import torch
    except ImportError:
        return {"available": False, "device": None, "message": "ultralytics not installed - motion only"}
    device = "GPU (CUDA)" if torch.cuda.is_available() else "CPU"
    local = sorted(p.name for p in MODELS_DIR.glob("*.pt")) if MODELS_DIR.is_dir() else []
    return {"available": True, "device": device, "local_models": local, "message": f"Runs on {device}"}
