"""Index of the folders that already exist under the configured library roots."""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..models import LibraryItem, utcnow
from .metadata import similarity
from .parser import TECH_TOKENS, parse as parse_release, title_key

_FOLDER_YEAR = re.compile(r"^(?P<name>.+?)[\s._]*[\(\[](?P<year>19\d{2}|20\d{2})[\)\]]\s*$")
_SEASON_DIR = re.compile(r"(?i)^(?:season|staffel|s)[\s._-]*(?P<n>\d{1,3})$")
_SPECIALS_DIR = re.compile(r"(?i)^(?:specials?|extras?|ova|ovas)$")
_SKIP_DIRS = {"@eadir", ".recycle", "#recycle", "lost+found", ".stfolder", ".git"}


@dataclass
class FolderMatch:
    item: LibraryItem
    score: float
    reason: str


def split_folder_name(folder_name: str) -> tuple[str, int | None]:
    match = _FOLDER_YEAR.match(folder_name.strip())
    if match:
        return match.group("name").strip(" .-_"), int(match.group("year"))
    return folder_name.strip(" .-_"), None


def _looks_like_release(folder_name: str) -> bool:
    """Folders named after the release, e.g. 3.Idiots.2009.German.720p.BluRay.x264-Pate."""
    tokens = {token.lower() for token in re.split(r"[^A-Za-z0-9]+", folder_name) if token}
    return bool(tokens & TECH_TOKENS)


def folder_title(folder_name: str) -> tuple[str, int | None]:
    """Title and year of a library folder, release names included."""
    title, year = split_folder_name(folder_name)
    if _looks_like_release(folder_name):
        parsed = parse_release(folder_name)
        if parsed.title and len(parsed.title) >= 2:
            return parsed.title, parsed.year or year
    return title, year


def _seasons_in(path: Path) -> list[int]:
    return sorted(season_folders(path))


def season_folders(path: Path) -> dict[int, str]:
    """Season number to the folder name that is actually on disk (S1, S01, Season 01, Staffel 2)."""
    found: dict[int, str] = {}
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir():
            continue
        match = _SEASON_DIR.match(entry.name)
        if match:
            found.setdefault(int(match.group("n")), entry.name)
        elif _SPECIALS_DIR.match(entry.name):
            found.setdefault(0, entry.name)
    return found


def find_season_folder(series_dir: str | None, season: int | None) -> str | None:
    """Reuse the season folder that already exists instead of adding a second style."""
    if not series_dir or season is None:
        return None
    return season_folders(Path(series_dir)).get(season)


INDEX_STATE: dict[str, Any] = {"running": False, "finished_at": None, "roots": {}, "error": None}


def folder_activity(path: Path, max_depth: int = 2) -> tuple[dt.datetime | None, int]:
    """When something was last added to a folder, and how many video files it holds.

    Directory mtimes are enough: adding an episode touches the folder it lands in.
    The depth cap keeps a library with a few hundred titles at a few seconds.
    """
    newest = 0.0
    videos = 0
    base_depth = len(path.parts)
    video_extensions = tuple(config.get("video_extensions"))
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        if len(Path(dirpath).parts) - base_depth >= max_depth:
            dirnames[:] = []
        try:
            newest = max(newest, os.stat(dirpath).st_mtime)
        except OSError:
            continue
        videos += sum(1 for name in filenames if name.lower().endswith(video_extensions))
    if not newest:
        return None, videos
    return dt.datetime.fromtimestamp(newest, dt.timezone.utc).replace(tzinfo=None), videos


def reindex(session: Session) -> dict[str, int]:
    """Rebuild the folder index. Missing roots are reported, never fatal."""
    INDEX_STATE["running"] = True
    INDEX_STATE["error"] = None
    try:
        stats = _reindex(session)
        INDEX_STATE["roots"] = stats
        return stats
    except Exception as exc:  # noqa: BLE001
        INDEX_STATE["error"] = str(exc)
        raise
    finally:
        INDEX_STATE["running"] = False
        INDEX_STATE["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()


def _reindex(session: Session) -> dict[str, int]:
    stats: dict[str, int] = {}
    session.execute(delete(LibraryItem))
    for root, media_type in config.library_roots():
        root_path = Path(root)
        if not root_path.is_dir():
            stats[root] = -1
            continue
        count = 0
        try:
            entries = sorted(os.scandir(root_path), key=lambda e: e.name.lower())
        except OSError:
            stats[root] = -1
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith(".") or entry.name.lower() in _SKIP_DIRS:
                continue
            title, year = folder_title(entry.name)
            last_added, file_count = folder_activity(Path(entry.path))
            session.add(
                LibraryItem(
                    root=root,
                    path=entry.path,
                    folder_name=entry.name,
                    title=title,
                    title_key=title_key(title),
                    year=year,
                    media_type=media_type,
                    seasons=_seasons_in(Path(entry.path)),
                    last_added=last_added,
                    file_count=file_count,
                    indexed_at=utcnow(),
                )
            )
            count += 1
        stats[root] = count
    session.flush()
    return stats


def find_folder(
    session: Session,
    titles: list[str],
    media_type: str,
    year: int | None = None,
    threshold: float = 0.88,
) -> FolderMatch | None:
    """Find an existing folder for any of the given titles.

    An existing folder always wins over the configured default path, which is the
    whole point for the two anime locations.
    """
    wanted = {title_key(title) for title in titles if title}
    wanted.discard("")
    if not wanted:
        return None

    preferred_types = {"anime", "series"} if media_type in {"anime", "series"} else {"movie"}
    items = list(session.scalars(select(LibraryItem)))
    exact = [item for item in items if item.title_key in wanted]
    if exact:
        best = _pick(exact, preferred_types, media_type, year)
        return FolderMatch(item=best, score=1.0, reason="Ordnername stimmt genau")

    best_item: LibraryItem | None = None
    best_score = 0.0
    for item in items:
        if item.media_type not in preferred_types:
            continue
        score = max(similarity(item.title, title) for title in titles if title)
        if year and item.year and abs(item.year - year) > 1:
            score -= 0.1
        if score > best_score:
            best_item, best_score = item, score
    if best_item is not None and best_score >= threshold:
        return FolderMatch(item=best_item, score=round(best_score, 3), reason="Ordnername stimmt ungefähr")
    return None


def _pick(items: list[LibraryItem], preferred_types: set[str], media_type: str, year: int | None) -> LibraryItem:
    def rank(item: LibraryItem) -> tuple[int, int, int]:
        return (
            1 if item.media_type == media_type else 0,
            1 if item.media_type in preferred_types else 0,
            1 if year and item.year == year else 0,
        )

    return sorted(items, key=rank, reverse=True)[0]


def default_root(media_type: str) -> str:
    """Where a title lands that has no folder anywhere yet."""
    if media_type == "anime":
        return config.get("default_anime_path") or config.get("anime_path_1")
    if media_type == "movie":
        return config.get("default_movie_path") or config.get("movies_path")
    return config.get("default_series_path") or config.get("series_path")


def known_roots_for(media_type: str) -> list[str]:
    return [root for root, kind in config.library_roots() if kind == media_type]
