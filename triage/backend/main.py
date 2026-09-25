from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import cv2
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import file_browser
from analyse import AnalysisSettings, zones_from_dicts
from detector import CATEGORIES, STOCK_MODELS, backend_status
from motion import SENSITIVITY

from .sessions import Session, read_first_frame, session_manager

app = FastAPI(title="Triage")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _get_session(session_id: str) -> Session:
    session = session_manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found - reload the folder")
    return session


def _get_file_index(session: Session, index: int) -> int:
    if not 0 <= index < len(session.files):
        raise HTTPException(status_code=404, detail="File not found")
    return index


def _jpeg_response(data: bytes) -> Response:
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
async def api_status():
    return backend_status()


@app.get("/api/browse")
async def api_browse(path: str = Query(default="")):
    try:
        return file_browser.browse(path or None)
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class SessionRequest(BaseModel):
    folder: str


@app.post("/api/sessions")
async def api_create_session(req: SessionRequest):
    try:
        session = session_manager.create(req.folder)
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not session.files:
        raise HTTPException(status_code=400, detail="No recognised video files found in that folder")
    return session.public_dict()


@app.get("/api/sessions/{session_id}")
async def api_session(session_id: str):
    return _get_session(session_id).public_dict()


@app.get("/api/sessions/{session_id}/files/{index}/frame.jpg")
def api_frame(session_id: str, index: int):
    """A clip's first frame, for drawing zones on."""
    session = _get_session(session_id)
    path = session.files[_get_file_index(session, index)].path
    frame = read_first_frame(path)
    if frame is None:
        raise HTTPException(status_code=422, detail=f"Couldn't read a frame from {path.name}")
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise HTTPException(status_code=500, detail="JPEG encode failed")
    return _jpeg_response(buf.tobytes())


@app.get("/api/sessions/{session_id}/files/{index}/heatmap.jpg")
def api_heatmap(session_id: str, index: int):
    """Where in the scene motion happened over the whole clip."""
    session = _get_session(session_id)
    data = session.heatmap_jpeg(_get_file_index(session, index))
    if not data:
        raise HTTPException(status_code=404, detail="No motion heatmap for this file yet")
    return _jpeg_response(data)


@app.get("/api/sessions/{session_id}/events/{event_id}.jpg")
def api_event_snapshot(session_id: str, event_id: str, thumb: bool = False):
    session = _get_session(session_id)
    pair = session.snapshots.get(event_id)
    if not pair or not pair[0]:
        raise HTTPException(status_code=404, detail="No snapshot for this event")
    # ids are unique per run, so the browser can keep these
    return Response(pair[1] if thumb else pair[0], media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


class ZoneIn(BaseModel):
    x: int
    y: int
    w: int = Field(..., ge=5)
    h: int = Field(..., ge=5)
    mode: str = Field("watch", pattern="^(watch|ignore)$")
    name: str = ""


class AnalyseRequest(BaseModel):
    zones: list[ZoneIn] = []
    sensitivity: str = "medium"
    sample_fps: float = Field(5.0, gt=0, le=30)
    detect_objects: bool = True
    categories: list[str] = ["person", "vehicle", "weapon", "bag"]
    model: str = "n"
    extra_model: str = ""
    confidence: float = Field(0.4, ge=0.05, le=0.95)
    detect_every: float = Field(1.0, gt=0, le=30)
    motion_gated: bool = True


@app.post("/api/sessions/{session_id}/process")
async def api_process(session_id: str, req: AnalyseRequest):
    session = _get_session(session_id)
    if session.status == "running":
        raise HTTPException(status_code=409, detail="Already analysing")
    if req.sensitivity not in SENSITIVITY:
        raise HTTPException(status_code=400, detail=f"Unknown sensitivity: {req.sensitivity}")
    if req.model not in STOCK_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model size: {req.model}")
    unknown = set(req.categories) - set(CATEGORIES)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown categories: {', '.join(sorted(unknown))}")
    settings = AnalysisSettings(
        sensitivity=req.sensitivity,
        sample_fps=req.sample_fps,
        zones=zones_from_dicts([z.model_dump() for z in req.zones]),
        detect_objects=req.detect_objects,
        categories=set(req.categories),
        confidence=req.confidence,
        detect_every=req.detect_every,
        motion_gated=req.motion_gated,
    )
    session.start_processing(settings, req.model, req.extra_model.strip().strip('"'))
    return session.public_dict()


@app.post("/api/sessions/{session_id}/cancel")
async def api_cancel(session_id: str):
    session = _get_session(session_id)
    if session.status != "running":
        raise HTTPException(status_code=400, detail="Nothing is running")
    session.cancel_event.set()
    return {"cancelled": True}


@app.post("/api/sessions/{session_id}/files/{index}/open")
async def api_open_file(session_id: str, index: int):
    """Open a clip in the system's default video player (the server runs on
    the user's own machine, bound to localhost)."""
    session = _get_session(session_id)
    path = session.files[_get_file_index(session, index)].path
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606 - a file from the user's own loaded folder
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Couldn't open {path.name}: {exc}") from exc
    return {"opened": True}


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


@app.get("/api/sessions/{session_id}/export.csv")
async def api_export_csv(session_id: str):
    session = _get_session(session_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["file", "type", "start", "end", "start_seconds", "end_seconds",
                     "peak", "max_count", "labels", "zones"])
    for f in session.files:
        for ev in f.events:
            peak = f"{ev['peak'] * 100:.1f}% of frame" if ev["kind"] in ("motion", "scene_change") else f"{ev['peak']:.2f}"
            writer.writerow([
                f.path.name, ev["kind"], _clock(ev["start"]), _clock(ev["end"]),
                ev["start"], ev["end"], peak, ev["count"] or "",
                "; ".join(ev["labels"]), "; ".join(ev["zones"]),
            ])
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", session.folder.name) or "export"
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="triage-{stem}.csv"'},
    )


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
