"""Server-side directory listing so the browser UI can navigate the local filesystem
without relying on <input type=file>, which hides the real path from web pages."""
from __future__ import annotations

import os
import string
from pathlib import Path

# Common CCTV/DVR export containers - includes the raw/proprietary ones
# (.dav, .264) that general video tools often skip.
VIDEO_EXTENSIONS = {
    ".mp4", ".avi", ".mov", ".mkv", ".asf", ".wmv", ".ts", ".dav",
    ".264", ".h264", ".mpg", ".mpeg", ".m4v", ".webm",
}


def list_drives() -> list[str]:
    if os.name != "nt":
        return ["/"]
    drives = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            drives.append(drive)
    return drives


def list_videos(folder: str | Path) -> list[Path]:
    p = Path(folder)
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")
    return sorted(
        (c for c in p.iterdir() if c.is_file() and c.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda c: c.name.lower(),
    )


def browse(path: str | None) -> dict:
    """List sub-folders (navigable) and video files (shown so the user can tell
    they're in the right folder) of `path`, or the drive roots if empty."""
    if not path:
        entries = [{"name": d, "path": d, "type": "dir"} for d in list_drives()]
        return {"path": "", "parent": None, "entries": entries, "video_count": 0}

    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    entries = []
    try:
        children = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError:
        children = []

    video_count = 0
    for child in children:
        try:
            if child.is_dir():
                entries.append({"name": child.name, "path": str(child), "type": "dir"})
            elif child.suffix.lower() in VIDEO_EXTENSIONS:
                video_count += 1
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "type": "file",
                    "size": child.stat().st_size,
                })
        except (PermissionError, OSError):
            continue

    parent = str(p.parent) if p.parent != p else None
    if os.name == "nt" and len(str(p)) <= 3:
        parent = ""  # drive root -> back to the drive list

    return {"path": str(p), "parent": parent, "entries": entries, "video_count": video_count}
