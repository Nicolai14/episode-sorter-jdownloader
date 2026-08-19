"""Technical details of a video file, used for duplicate comparison."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import files


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe(path: Path) -> dict[str, Any]:
    """Return resolution, codecs and languages. Falls back to size only."""
    info: dict[str, Any] = {"path": str(path), "size": 0, "size_human": None}
    try:
        info["size"] = path.stat().st_size
        info["size_human"] = files.human_size(info["size"])
    except OSError:
        return info

    if not ffprobe_available():
        info["note"] = "ffprobe is not installed"
        return info

    command = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        output = subprocess.run(command, capture_output=True, timeout=45, check=True)
        payload = json.loads(output.stdout or b"{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        info["note"] = f"ffprobe failed: {exc}"
        return info

    audio: list[str] = []
    subtitles: list[str] = []
    for stream in payload.get("streams", []):
        kind = stream.get("codec_type")
        language = (stream.get("tags") or {}).get("language") or "und"
        if kind == "video" and "width" not in info:
            info["width"] = stream.get("width")
            info["height"] = stream.get("height")
            info["video_codec"] = stream.get("codec_name")
            info["resolution"] = f"{stream.get('width')}x{stream.get('height')}"
        elif kind == "audio":
            audio.append(f"{language}/{stream.get('codec_name')}")
        elif kind == "subtitle":
            subtitles.append(language)
    info["audio"] = audio
    info["subtitles"] = subtitles
    fmt = payload.get("format") or {}
    if fmt.get("duration"):
        info["duration_minutes"] = round(float(fmt["duration"]) / 60, 1)
    if fmt.get("bit_rate"):
        info["bitrate_kbps"] = round(int(fmt["bit_rate"]) / 1000)
    return info


def quality_label(info: dict[str, Any]) -> str:
    height = info.get("height")
    if not height:
        return "unknown"
    for limit, label in ((2000, "2160p"), (1000, "1080p"), (700, "720p"), (500, "576p"), (0, "480p")):
        if height >= limit:
            return label
    return "unknown"
