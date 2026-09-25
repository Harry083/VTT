# Triage — CCTV Motion & Object Finder

A local web app that goes through a folder of CCTV exports and reports
**where and when things move**, and **what's there**: people, vehicles,
weapons, bags and animals. It lays each clip out on an activity timeline so
you can go straight to the parts worth watching.

A standalone companion to VTT (same look, same folder workflow). It's meant
to be folded into VTT later. For now it runs on its own port, so the two can
run side by side.

## Setup

Requires Python 3.10+.

```bash
cd triage
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`ultralytics` pulls in PyTorch (CPU build by default). For an NVIDIA GPU,
install the CUDA build of torch first
([pytorch.org/get-started](https://pytorch.org/get-started)); detection is
several times faster there. Everything else works without it: if
`ultralytics` isn't installed, the app runs motion detection only.

The YOLO weights (`yolo11n.pt`, ~5 MB) download into `triage/models/` the
first time object detection runs. On an offline machine, copy the `.pt`
files into `triage/models/` by hand.

## Run

```bash
.venv\Scripts\python.exe run.py
```

Then open http://localhost:8758

1. **Folder**: paste a path or use *Browse…* to pick the folder of exports.
2. **Zones** (optional): drag boxes on a preview frame.
   - *Watch* zones: only motion and objects inside them are reported
     (a doorway, a car park).
   - *Ignore* zones: nothing there is reported. Use them for the
     burned-in clock, trees, flags or a busy road in the background.
   Zones are kept when you load another folder from the same camera.
3. **Motion**: *Sensitivity* sets how small or subtle a movement counts.
   *Frames checked* trades speed for catching very brief movement.
4. **Objects**: choose the model size, the minimum confidence, how often
   to look, and which kinds of object to report. *Only around motion* (on by
   default) runs the detector only while something is moving. That is much
   faster on long, quiet footage, but it won't report a parked car or
   someone standing completely still for the whole clip.
5. **Analyse Files**. Results fill in as each file finishes:
   - **Activity timeline**: one row per clip, with time from the start of
     the clip. The blue band is motion level; the coloured lanes underneath
     are sightings of each object type. Hover for details, click a bar to
     open that event, click a file name for its motion heatmap.
   - **Events**: every motion event and sighting with a snapshot, time in
     the clip, length and confidence. Click one to see the full snapshot
     with boxes drawn, then step through with ← →. *Open Clip* plays it in
     your default player.
6. **Export CSV**: one row per event.

## Weapons

The stock YOLO models are trained on COCO, whose only weapon classes are
*knife* and *baseball bat*. They **will not detect firearms**. To detect
them, put the path of a YOLO model trained for weapons in *Extra model*.
Any Ultralytics `.pt` / `.onnx` works, for example one fine-tuned on a
public firearms dataset. It runs alongside the stock model on the same
frames, and its classes are sorted by name: anything containing *gun*,
*pistol*, *rifle*, *firearm*, *weapon*, *revolver*, *knife*, *machete*,
*sword* (or the words *blade*, *axe*, *bat*, *crowbar*, *hammer*) counts as
a **weapon**. Person and vehicle classes are recognised the same way.

Treat weapon hits as leads to check, not findings. Phones, tools and
umbrellas get flagged, and small or partly hidden weapons get missed.

## Pipeline

The web app (`backend/` + `frontend/`) is a thin layer over the pipeline
modules, which can also be used on their own:

1. **`motion.py`**: `MotionDetector`. Frames are downscaled to 480 px wide
   and fed to OpenCV's MOG2 background model. It uses colour, not
   greyscale: a red coat on grey paving is nearly the same brightness.
   Shadows are dropped, the mask is clipped to the zones and cleaned up,
   and blobs above a minimum size become boxes. A change covering more
   than 55% of the frame at once is reported as a *lighting change* (IR
   switching, lights on, the camera being knocked), not as motion. It also
   accumulates the per-clip motion heatmap.
2. **`detector.py`**: `ObjectDetector` wraps one or two Ultralytics YOLO
   models and sorts their class names into person / vehicle / weapon / bag /
   animal. Loaded once per server process and reused.
3. **`events.py`**: `EventBuilder` joins per-frame observations into
   events. Samples of the same kind less than a couple of seconds apart are
   one event, so someone passing behind a pillar stays one sighting. It
   keeps the strongest frame as the snapshot.
4. **`analyse.py`**: `analyse_video(path, settings, detector)` wires the
   three together for one clip. It samples frames, runs motion on every
   sample, and runs object detection on the samples worth looking at.
5. **`backend/`**: FastAPI app (`main.py`), per-folder sessions and the
   background worker thread (`sessions.py`), and the folder browser
   (`file_browser.py`, shared with VTT). **`frontend/`** is the vanilla
   HTML/CSS/JS UI, in VTT's style.

```python
from pathlib import Path
from analyse import AnalysisSettings, analyse_video, zones_from_dicts
from detector import get_detector

settings = AnalysisSettings(
    zones=zones_from_dicts([{"x": 0, "y": 0, "w": 420, "h": 45, "mode": "ignore"}]),
    categories={"person", "vehicle", "weapon"},
)
clip = analyse_video(Path("cam1.mp4"), settings, get_detector("n"))
for ev in clip.events:
    print(ev.kind, ev.start, ev.end, ev.peak, sorted(ev.labels))
```

## Folding into VTT

Events carry times in **seconds from the start of the clip**. VTT already
works out each clip's wall-clock start from the burned-in timestamp, so
integrating means adding that start time to each event's offset. Events then
land on VTT's timeline, and a sighting can be given as an actual time of day.
Both apps use the same session/polling/folder-browser structure, so the
backend modules can move across as they are.

## Project structure

```
triage/
├── backend/
│   ├── main.py            FastAPI app & routes
│   ├── sessions.py        per-folder state, background analysis
│   └── file_browser.py    server-side directory listing for the folder picker
├── frontend/              vanilla HTML/CSS/JS UI
├── models/                YOLO weights (downloaded on first use, or copied in)
├── motion.py              background-subtraction motion detection + heatmap
├── detector.py            YOLO wrapper, class name → category
├── events.py              per-frame observations → timed events
├── analyse.py             sample → motion → objects → events for one clip
├── run.py                 web app entry point (uvicorn, port 8758)
└── requirements.txt
```
