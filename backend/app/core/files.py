"""Filesystem helpers: classification, stability checks, companion files."""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from .. import config

_LANG_SUFFIX = re.compile(
    r"(?i)[._-](?P<lang>de|ger|german|deutsch|en|eng|english|jp|jpn|ja|japanese|forced|sdh|full)$"
)
_LANG_MAP = {
    "ger": "de", "german": "de", "deutsch": "de", "de": "de",
    "eng": "en", "english": "en", "en": "en",
    "jpn": "ja", "japanese": "ja", "jp": "ja", "ja": "ja",
}
_ARCHIVE_MARKERS = (".part", ".part1", ".!ut", ".tmp", ".jdtmp", ".crdownload")
_ARCHIVE_EXTS = (".rar", ".r00", ".zip", ".7z", ".001", ".tar", ".gz")


def is_video(path: Path) -> bool:
    return path.suffix.lower() in set(config.get("video_extensions"))


def is_subtitle(path: Path) -> bool:
    return path.suffix.lower() in set(config.get("subtitle_extensions"))


def is_ignored(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith("."):
        return True
    if path.suffix.lower() in set(config.get("ignored_extensions")):
        return True
    for term in config.get("ignored_terms"):
        term = str(term).lower().strip()
        if term and re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", name):
            return True
    return False


def looks_incomplete(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(marker) for marker in _ARCHIVE_MARKERS)


def extraction_in_progress(directory: Path) -> bool:
    """True while JDownloader still writes into or extracts inside a folder."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return True
    for entry in entries:
        low = entry.name.lower()
        if any(low.endswith(marker) for marker in _ARCHIVE_MARKERS):
            return True
        if low.endswith(_ARCHIVE_EXTS):
            # An archive that was written in the last two minutes is probably still being extracted.
            try:
                if time.time() - entry.stat().st_mtime < 120:
                    return True
            except OSError:
                return True
    return False


def is_stable(path: Path, last_size: int) -> tuple[bool, int]:
    """Compare the current size against the previously seen one."""
    try:
        size = path.stat().st_size
    except OSError:
        return False, 0
    return size == last_size and size > 0, size


def min_video_bytes() -> int:
    return int(config.get("min_video_size_mb", 80)) * 1024 * 1024


def find_companions(video: Path) -> list[dict[str, str]]:
    """Subtitles that belong to a video file, with the detected language tag."""
    if not config.get("move_subtitles", True):
        return []
    stem = video.stem.lower()
    found: list[dict[str, str]] = []
    try:
        entries = list(os.scandir(video.parent))
    except OSError:
        return found
    for entry in entries:
        candidate = Path(entry.path)
        if not entry.is_file() or not is_subtitle(candidate):
            continue
        candidate_stem = candidate.stem.lower()
        if not candidate_stem.startswith(stem[: max(8, len(stem) - 12)]):
            continue
        suffix = ""
        match = _LANG_SUFFIX.search(candidate.stem)
        if match:
            raw = match.group("lang").lower()
            suffix = _LANG_MAP.get(raw, raw)
        found.append({"path": str(candidate), "lang": suffix, "ext": candidate.suffix.lower()})
    # Subtitle folders next to the video (common in German releases).
    for sub_dir in ("subs", "subtitles", "untertitel"):
        directory = video.parent / sub_dir
        if directory.is_dir():
            for entry in sorted(os.scandir(directory), key=lambda e: e.name):
                candidate = Path(entry.path)
                if entry.is_file() and is_subtitle(candidate):
                    match = _LANG_SUFFIX.search(candidate.stem)
                    lang = _LANG_MAP.get(match.group("lang").lower(), match.group("lang").lower()) if match else ""
                    found.append({"path": str(candidate), "lang": lang, "ext": candidate.suffix.lower()})
    return found


def free_space_mb(path: Path) -> int:
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return int(shutil.disk_usage(target).free / (1024 * 1024))
    except OSError:
        return 0


def same_filesystem(left: Path, right: Path) -> bool:
    def device(path: Path) -> int | None:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            return os.stat(probe).st_dev
        except OSError:
            return None

    left_dev, right_dev = device(left), device(right)
    return left_dev is not None and left_dev == right_dev


def remove_empty_dirs(directory: Path, stop_at: Path) -> None:
    """Clean up the download folder after a successful move."""
    if not config.get("delete_empty_source_dirs", True):
        return
    current = directory
    while current != stop_at and stop_at in current.parents:
        try:
            remaining = [entry for entry in os.scandir(current) if not is_ignored(Path(entry.path))]
        except OSError:
            return
        if remaining:
            return
        try:
            shutil.rmtree(current)
        except OSError:
            return
        current = current.parent


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
