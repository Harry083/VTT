from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import cv2
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import file_browser
from timestamp_parser import parse_timestamp

from .sessions import Session, crop, read_frame, session_manager

app = FastAPI(title="VTT")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

DATE_ORDERS = {"DMY", "MDY", "YMD"}
HOUR_FORMATS = {"24", "12"}
FORMAT_EXAMPLES = {"DMY": "DD-MM-YYYY", "MDY": "MM-DD-YYYY", "YMD": "YYYY-MM-DD"}


def _get_session(session_id: str) -> Session:
    session = session_manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found - reload the folder")
    return session


def _get_file_index(session: Session, index: int) -> int:
    if not 0 <= index < len(session.files):
        raise HTTPException(status_code=404, detail="File not found")
    return index


def _validate_settings(region: list[int], date_order: str, hour_format: str) -> tuple:
    if len(region) != 4 or region[2] < 5 or region[3] < 5:
        raise HTTPException(status_code=400, detail="Region must be [x, y, w, h] and at least 5×5 px")
    if date_order not in DATE_ORDERS:
        raise HTTPException(status_code=400, detail=f"Unknown date order: {date_order}")
    if hour_format not in HOUR_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unknown hour format: {hour_format}")
    return tuple(int(v) for v in region)


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
def api_frame(
    session_id: str,
    index: int,
    which: str = Query(default="first", pattern="^(first|last)$"),
    cropped: bool = False,
):
    """A clip's first/last frame as JPEG - the full frame for the region
    picker, or just the timestamp region for reviewing OCR results."""
    session = _get_session(session_id)
    path = session.files[_get_file_index(session, index)].path
    frame = read_frame(path, which)
    if frame is None:
        raise HTTPException(status_code=422, detail=f"Couldn't read a frame from {path.name}")
    if cropped:
        if not session.region:
            raise HTTPException(status_code=400, detail="No timestamp region set")
        frame = crop(frame, session.region)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise HTTPException(status_code=500, detail="JPEG encode failed")
    return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class OcrSettings(BaseModel):
    region: list[int] = Field(..., description="[x, y, w, h] in original frame pixels")
    date_order: str = "DMY"
    hour_format: str = "24"


class TestRequest(OcrSettings):
    index: int = 0


@app.post("/api/sessions/{session_id}/test")
def api_test_ocr(session_id: str, req: TestRequest):
    session = _get_session(session_id)
    index = _get_file_index(session, req.index)
    region = _validate_settings(req.region, req.date_order, req.hour_format)
    try:
        return session.test_ocr(index, region, req.date_order, req.hour_format)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as-is
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sessions/{session_id}/process")
async def api_process(session_id: str, req: OcrSettings):
    session = _get_session(session_id)
    if session.status == "running":
        raise HTTPException(status_code=409, detail="Already processing")
    region = _validate_settings(req.region, req.date_order, req.hour_format)
    session.start_processing(region, req.date_order, req.hour_format)
    return session.public_dict()


@app.post("/api/sessions/{session_id}/cancel")
async def api_cancel(session_id: str):
    session = _get_session(session_id)
    if session.status != "running":
        raise HTTPException(status_code=400, detail="Nothing is running")
    session.cancel_event.set()
    return {"cancelled": True}


class ManualEdit(BaseModel):
    start: str
    end: str
    # typed in the same format as the on-screen timestamp (the pickers)
    date_order: str = "DMY"
    hour_format: str = "24"


def _parse_manual(text: str, label: str, date_order: str, hour_format: str) -> datetime:
    dt = parse_timestamp(text, date_order, hour_format)
    example = FORMAT_EXAMPLES[date_order] + (" hh:mm:ss AM" if hour_format == "12" else " HH:MM:SS")
    if dt is None:
        raise HTTPException(status_code=400, detail=f"Couldn't read the {label} time - use {example}")
    if hour_format == "12" and not re.search(r"[AaPp]\.?[Mm]", text):
        raise HTTPException(status_code=400, detail=f"Add AM or PM to the {label} time")
    return dt


@app.put("/api/sessions/{session_id}/files/{index}")
async def api_manual_edit(session_id: str, index: int, req: ManualEdit):
    session = _get_session(session_id)
    index = _get_file_index(session, index)
    if req.date_order not in DATE_ORDERS or req.hour_format not in HOUR_FORMATS:
        raise HTTPException(status_code=400, detail="Unknown date/time format")
    start = _parse_manual(req.start, "start", req.date_order, req.hour_format)
    end = _parse_manual(req.end, "end", req.date_order, req.hour_format)
    if end < start:
        raise HTTPException(status_code=400, detail="End is before start")
    session.set_manual(index, start, end)
    return session.files[index].public_dict(index)


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


@app.get("/api/sessions/{session_id}/export.csv")
async def api_export_csv(session_id: str):
    session = _get_session(session_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["file", "start", "end", "confidence", "status"])
    for r in sorted(session.files, key=lambda r: r.start_dt or datetime.max):
        writer.writerow([
            r.path.name,
            r.start_dt.isoformat() if r.start_dt else r.start_raw or "",
            r.end_dt.isoformat() if r.end_dt else r.end_raw or "",
            f"{r.confidence:.2f}",
            "manual" if r.manual else r.status,
        ])
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", session.folder.name) or "export"
    filename = f"vtt-{stem}.csv"
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
