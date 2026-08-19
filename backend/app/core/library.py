"""Index of the folders that already exist under the configured library roots."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..models import LibraryItem, utcnow
from .metadata import similarity
from .parser import title_key

_FOLDER_YEAR = re.compile(r"^(?P<name>.+?)[\s._]*[\(\[](?P<year>19\d{2}|20\d{2})[\)\]]\s*$")
_SEASON_DIR = re.compile(r"(?i)^(season|staffel)[\s._-]*(?P<n>\d{1,3})$")
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


def _seasons_in(path: Path) -> list[int]:
    seasons: list[int] = []
    try:
        for entry in os.scandir(path):
            if not entry.is_dir():
                continue
            match = _SEASON_DIR.match(entry.name)
            if match:
                seasons.append(int(match.group("n")))
            elif entry.name.lower() in {"specials", "special"}:
                seasons.append(0)
    except OSError:
        return seasons
    return sorted(set(seasons))


def reindex(session: Session) -> dict[str, int]:
    """Rebuild the folder index. Missing roots are reported, never fatal."""
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
            title, year = split_folder_name(entry.name)
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
        return FolderMatch(item=best, score=1.0, reason="exact folder name match")

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
        return FolderMatch(item=best_item, score=round(best_score, 3), reason="fuzzy folder name match")
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
    if media_type == "anime":
        return config.get("default_anime_path") or config.get("anime_path_1")
    if media_type == "movie":
        return config.get("movies_path")
    return config.get("series_path")


def known_roots_for(media_type: str) -> list[str]:
    return [root for root, kind in config.library_roots() if kind == media_type]
