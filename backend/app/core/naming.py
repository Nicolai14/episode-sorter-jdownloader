"""Target path construction from the configured templates."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import config

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING = re.compile(r"[ .]+$")


def sanitize(value: str) -> str:
    cleaned = _INVALID.sub("", value or "").strip()
    cleaned = cleaned.replace("  ", " ")
    cleaned = _TRAILING.sub("", cleaned)
    return cleaned[:180] or "Unknown"


def folder_name(title: str, year: int | None) -> str:
    base = sanitize(title)
    return f"{base} ({year})" if year else base


@dataclass
class TargetPlan:
    directory: str
    filename: str
    path: str
    season_folder: str | None
    used_existing_folder: bool
    notes: list[str]


def _format(template: str, **values) -> str:
    class _Safe(dict):
        def __missing__(self, key: str) -> str:  # keep unknown placeholders visible
            return "{" + key + "}"

    try:
        return template.format_map(_Safe(**values))
    except (ValueError, KeyError):
        return template


def build_plan(
    *,
    media_type: str,
    title: str,
    year: int | None,
    season: int | None,
    episode: int | None,
    episode_end: int | None,
    extension: str,
    base_dir: str,
    existing_folder: str | None = None,
    special_kind: str | None = None,
    season_folder_override: str | None = None,
) -> TargetPlan:
    notes: list[str] = []
    title_clean = sanitize(title)
    if existing_folder:
        directory = Path(existing_folder)
        notes.append(f"Vorhandener Ordner verwendet: {existing_folder}")
    else:
        directory = Path(base_dir) / folder_name(title_clean, year)

    if media_type == "movie":
        filename = sanitize(_format(config.get("movie_template"), title=title_clean, year=year or "")) + extension
        return TargetPlan(
            directory=str(directory),
            filename=filename,
            path=str(directory / filename),
            season_folder=None,
            used_existing_folder=bool(existing_folder),
            notes=notes,
        )

    season_number = 1 if season is None else season
    if season_number == 0 or special_kind in {"special", "ova", "ona"}:
        season_number = 0
        season_folder = config.get("specials_folder", "Specials")
    else:
        season_folder = _format(config.get("season_folder_template"), season=season_number)
    if season_folder_override:
        season_folder = season_folder_override
        notes.append(f"Vorhandener Staffelordner verwendet: {season_folder_override}")
    if season is None:
        notes.append("Keine Staffel im Dateinamen, Staffel 01 angenommen")

    values = {
        "title": title_clean,
        "year": year or "",
        "season": season_number,
        "episode": episode or 0,
        "episode_end": episode_end or 0,
    }
    template = config.get("episode_range_template") if episode_end else config.get("episode_template")
    stem = sanitize(_format(template, **values))
    if not year:
        stem = stem.replace(" ()", "")
    directory = directory / sanitize(season_folder)
    filename = stem + extension
    return TargetPlan(
        directory=str(directory),
        filename=filename,
        path=str(directory / filename),
        season_folder=sanitize(season_folder),
        used_existing_folder=bool(existing_folder),
        notes=notes,
    )


def companion_name(video_filename: str, lang: str, extension: str) -> str:
    stem = Path(video_filename).stem
    suffix = f".{lang}" if lang else ""
    return f"{stem}{suffix}{extension}"
