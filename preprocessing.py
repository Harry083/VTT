"""
preprocessing.py

Lightweight, format-agnostic preprocessing for burned-in CCTV timestamp
regions. CCTV overlays vary a lot: white text, black text, green text, or a
mixture within one clip (e.g. white date + green channel label). Rather than
hard-coding one colour assumption, this module generates a handful of cheap
binarisations covering the common cases and scores each on how "timestamp
shaped" it looks (using connected-component geometry, not OCR) so the OCR
step only has to run on the 1-2 most promising candidates.

All operations here are simple thresholding / morphology on a small crop,
so this comfortably runs per-file (not per-frame) without becoming a
bottleneck.
"""

from dataclasses import dataclass
import cv2
import numpy as np


# (min brightness, max saturation) pairs for the white-text candidates
WHITE_THRESHOLDS = [(180, 60), (205, 50), (225, 40), (240, 30)]


@dataclass
class Candidate:
    name: str
    image: np.ndarray  # binary, white text on black background
    score: float = 0.0


def upscale(img: np.ndarray, factor: float = 3.0) -> np.ndarray:
    """Small source crops are the single biggest cause of OCR failure."""
    h, w = img.shape[:2]
    return cv2.resize(img, (int(w * factor), int(h * factor)), interpolation=cv2.INTER_CUBIC)


def _score_candidate(binary: np.ndarray) -> float:
    """
    Heuristic score for how 'timestamp-like' a binary image is, based on
    connected-component count, height consistency and plausible glyph size -
    without running OCR. Higher is better. Returns 0.0 for images with too
    few plausible character-like components to be a timestamp.
    """
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    h, w = binary.shape[:2]

    comps = stats[1:]  # drop background label
    if len(comps) == 0:
        return 0.0

    heights = comps[:, cv2.CC_STAT_HEIGHT]
    areas = comps[:, cv2.CC_STAT_AREA]

    # plausible glyphs: tall enough to be a character, not a merged blob
    # covering most of the crop (that usually means the threshold picked up
    # background noise rather than text)
    mask = (heights > h * 0.25) & (heights < h * 0.95) & (areas < (h * w) * 0.5)
    plausible = comps[mask]

    if len(plausible) < 4:
        return 0.0

    ph = plausible[:, cv2.CC_STAT_HEIGHT].astype(float)
    height_consistency = 1.0 - (ph.std() / (ph.mean() + 1e-6))
    height_consistency = max(0.0, min(1.0, height_consistency))

    # timestamps are usually ~10-20 characters; components below ~25 chars
    # (separators like ':' often don't form distinct components, so we
    # centre the "ideal" a bit below the raw character count)
    count_score = 1.0 - abs(len(plausible) - 14) / 20.0
    count_score = max(0.0, min(1.0, count_score))

    return 0.6 * height_consistency + 0.4 * count_score


def _clean(binary: np.ndarray) -> np.ndarray:
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    return binary


def _keep_text_line(binary: np.ndarray) -> np.ndarray:
    """
    Blank everything outside the band (and horizontal extent) the glyphs sit
    in. On overlays without a backing plate, bright background specks above,
    below and beside the text otherwise get read as extra characters.
    """
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    comps = stats[1:]
    h = binary.shape[0]
    heights = comps[:, cv2.CC_STAT_HEIGHT]
    plausible = comps[(heights > h * 0.25) & (heights < h * 0.95)]
    if len(plausible) < 4:
        return binary

    glyph_h = float(np.median(plausible[:, cv2.CC_STAT_HEIGHT]))
    glyphs = plausible[np.abs(plausible[:, cv2.CC_STAT_HEIGHT] - glyph_h) < glyph_h * 0.25]
    if len(glyphs) < 4:
        return binary

    margin = glyph_h * 0.15
    top = int(max(0, np.median(glyphs[:, cv2.CC_STAT_TOP]) - margin))
    bottom = int(min(h, np.median(glyphs[:, cv2.CC_STAT_TOP] + glyphs[:, cv2.CC_STAT_HEIGHT]) + margin))
    left = int(max(0, glyphs[:, cv2.CC_STAT_LEFT].min() - glyph_h))
    right = int(glyphs[:, cv2.CC_STAT_LEFT].max() + glyphs[:, cv2.CC_STAT_WIDTH].max() + glyph_h)

    out = np.zeros_like(binary)
    out[top:bottom, left:right] = binary[top:bottom, left:right]
    return out


def generate_candidates(crop_bgr: np.ndarray) -> list:
    """
    Produce binarisations covering the common CCTV overlay styles, each
    normalised to white-text-on-black, scored, and sorted best-first.
    """
    hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)

    raw_candidates = []

    # White / light-grey text - most common CCTV overlay style. Swept over
    # several brightness cut-offs: a loose one keeps anti-aliased/dimmer text
    # intact, while stricter ones drop bright background (sky, leaves, walls)
    # that would otherwise merge into the glyphs when the overlay has no
    # backing plate. extract.py OCRs them all and keeps whichever parses.
    for v_min, s_max in WHITE_THRESHOLDS:
        white_mask = cv2.inRange(hsv, (0, 0, v_min), (180, s_max, 255))
        raw_candidates.append(Candidate(f"white{v_min}", white_mask))

    # Black text on a light background plate
    _, black_mask = cv2.threshold(gray, 80, 255, cv2.THRESH_BINARY_INV)
    raw_candidates.append(Candidate("black", black_mask))

    # Green text - common on older DVR overlays
    green_mask = cv2.inRange(hsv, (35, 60, 60), (85, 255, 255))
    raw_candidates.append(Candidate("green", green_mask))

    # Generic adaptive fallback for mixed / unusual overlays (e.g. text
    # straddling a light/dark background within the same crop)
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 8
    )
    raw_candidates.append(Candidate("adaptive", adaptive))

    scored = []
    for c in raw_candidates:
        cleaned = _keep_text_line(_clean(c.image))
        scored.append(Candidate(c.name, cleaned, _score_candidate(cleaned)))

    return sorted(scored, key=lambda c: c.score, reverse=True)


def preprocess_timestamp_region(crop_bgr: np.ndarray, upscale_factor: float = 3.0, top_n: int = None) -> list:
    """
    Entry point: given a BGR crop of the on-screen timestamp region, return
    the binarised candidates (best-scoring first, top_n of them if given),
    upscaled and ready for OCR.

    The geometry score only decides the order they're tried in: extract.py
    stops as soon as two candidates agree on the same parsed timestamp, so
    on clean overlays only the first couple are ever OCR'd.
    """
    upscaled = upscale(crop_bgr, upscale_factor)
    candidates = generate_candidates(upscaled)
    return candidates[:top_n] if top_n else candidates
